# Airplane Tracker — Build Plan

Status: **draft, awaiting approval**. Nothing in here is built yet.
Research and live API checks were done on 2026-10-14. Re-check terms before each phase that adds a new source.

Personal ADS-B alert service. It runs in one Docker container on a home server and has a PWA web UI. It alerts on aircraft that match user-defined rules, both aircraft nearby now and aircraft predicted to pass over a zone soon.

---

## 1. Recommended stack

The design goal is **one small container, one process, no extra databases**.

| Layer | Choice | Why |
|---|---|---|
| Backend | **Python 3.12, FastAPI, uvicorn (single worker), asyncio** | One event loop runs the pollers, the evaluation loop and the HTTP API. Fast to iterate on. The prediction math is easy to read and test. |
| HTTP client | `httpx` (async, HTTP/2, gzip) | Both sources serve gzip; see §2.3 for sizes. |
| Storage | **SQLite** (WAL mode) via `aiosqlite`, plain SQL migrations | No DB server. It holds config, rules, alert history, caches and the imported aircraft DB (~620k rows). |
| Live state | In-process memory (dict of tracks) | Rebuilt within one poll after a restart. Only alert and cooldown state must persist, and it lives in SQLite. |
| Numerics | `numpy` (prediction ensemble only) | Vectorised trajectory sampling. It is the only "heavy" dependency (~30 MB in the image). |
| Validation | `pydantic` v2 | Rule and zone schemas, shared by the API and the engine. |
| Auth | Built-in single-user login (argon2id + HttpOnly session cookie), with an optional `AUTH_MODE=proxy` | See §1.1. |
| Frontend | **Svelte + Vite**, built to static files and served by FastAPI | Small bundle with no runtime framework. Good fit for the form-heavy rule and zone editors. |
| Map | **Leaflet** (~40 KB) with an OSM tile layer (tile URL is configurable) | Much lighter than MapLibre. Raster tiles are fine at this scale. |
| Live updates | Server-Sent Events | Simpler than WebSockets and works through any reverse proxy. |
| PWA | Web app manifest + small service worker (app-shell cache only) | Installable on iOS and Android, works on desktop. Needs HTTPS. |
| Tooling | `uv`, `ruff`, `mypy --strict`, `pytest`; `npm` + `svelte-check` for the frontend | |
| Container | Multi-stage build: node stage builds the UI, then `python:3.12-slim` runtime, non-root user | |

**Footprint targets:** image under ~200 MB, RSS under ~150 MB, CPU under 2% average at a 10 s poll interval. The compose file sets `mem_limit: 256m` and a healthcheck.

The leaner alternative is **Go**: one static binary, ~20–30 MB RAM, ~25 MB image. It costs more code for the same features and a less convenient numeric and test workflow. Python is recommended unless you would rather maintain Go (see Questions).

### 1.1 Authentication

- **Default: built-in single-user auth.**
  - The password hash (argon2id) is set on first run from an env var or a CLI command.
  - Sessions use a long-lived (30-day, sliding) HttpOnly, `Secure`, `SameSite=Lax` cookie. Login attempts are rate-limited.
  - Every route except `/healthz`, `/login`, the manifest and static icons requires a session.
  - Reason for the default: installed PWAs on iOS handle SSO redirect flows (Authelia, Authentik, Cloudflare Access) poorly. Service-worker fetches hit the redirect and break, and the PWA's cookie jar is separate from Safari's.
- **Optional: `AUTH_MODE=proxy`.** Trusts a `Remote-User`-style header from your reverse proxy, but only from configured proxy IPs. Use this if you already run forward-auth and it works with your phone.
- HTTPS is required in either mode. PWA install, geolocation ("use my location") and web push all need a secure context. TLS terminates at your existing reverse proxy; the app doesn't handle certificates.

---

## 2. Data sources

### 2.1 Live position sources (checked 2026-10-14)

| Source | Access | Rate limit | Terms | Live check | Verdict |
|---|---|---|---|---|---|
| **adsb.fi Open Data** — `opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{nm}` (max 250 nm; the old v2 path is deprecated) | No key | **1 req/s** on public endpoints. 400/401/403/404/429 responses count toward a **temporary IP block**. | Personal, **non-commercial** only. **Must cite adsb.fi and link to it.** As-is; may be cut off at any time. | 200 OK. Returns `r`, `t`, `desc`, `ownOp`, `year`, `nav_heading`, `nav_altitude_mcp`, `baro_rate`/`geom_rate`, `squawk`, `emergency`. | **Primary** |
| **adsb.lol** — `api.adsb.lol/v2/point/{lat}/{lon}/{nm}` (max 250 nm). Also has `/v2/mil`, `/v2/sqk/{code}`, `/v2/type/{t}`, `POST /api/0/routeset` | No key **today**. The docs say a key "will be required in the future", obtained by feeding. | Not published; "dynamic based on load". | Data is **ODbL**. Free to use; asks you to contact them for "production" use. | 200 OK with the same readsb-style JSON. `routeset` returned **HTTP 201 with an empty body** in testing. | **Secondary / failover** |
| **airplanes.live** — `api.airplanes.live/v2/point/...` | — | Docs said 1 req/s, non-commercial (docs dated 2023) | — | **HTTP 403**: "Please contact us at contact@airplanes.live…". Their API docs repo was archived in April 2026. | **Excluded** unless you email them and get approval. The source plug-in would be trivial since the format is identical. |
| **OpenSky Network** REST `/states/all?lamin..` | **OAuth2 client credentials** (basic auth no longer accepted). Token TTL 30 min. | Credits per day: anonymous 400, registered 4,000, active feeder 8,000. A bbox of ≤25 sq° costs 1 credit, up to 100 sq° costs 2. Resolution 5 s (10 s anonymous). | Non-commercial / research | Not polled (needs an account) | **Optional fallback only.** 4,000 credits/day means one small box every ~22 s at best. It has **no aircraft type or registration** (only an emitter category), so everything depends on local enrichment. |
| ADS-B Exchange | Paid (RapidAPI) | — | — | — | Excluded (not free) |
| **Your RTL-SDR** (later) | readsb/tar1090 `aircraft.json` over LAN | Unlimited | Yours | — | **Planned source plug-in.** Same JSON shape as adsb.fi and adsb.lol. |

**Coverage caveats.** All community aggregators depend on volunteer feeders. Coverage is good in most of the US and Europe and patchy in rural areas, mountains and over water. *Low-altitude* traffic near your house (helicopters, GA) is the most likely to be missing unless a feeder is nearby. Your own receiver fixes exactly that gap. Military aircraft often don't broadcast ADS-B positions; some appear only via MLAT (less accurate, flagged as such) or not at all. adsb.fi and adsb.lol do not filter LADD/PIA aircraft.

**Bonus for feeding:** feeding adsb.fi or adsb.lol unlocks feeder-only endpoints and protects you from adsb.lol's future key requirement.

### 2.2 Pluggable source interface

```python
class Source(Protocol):
    name: str
    kind: Literal["area", "local"]          # area = queried by circle; local = whatever a receiver hears
    async def fetch(self, q: AreaQuery | None) -> SourceBatch   # normalized Observations + fetch metadata
    limits: RateLimit                       # token bucket enforced by the poller, not the source
```

- Every source normalizes into one `Observation` dataclass. Units are **nm, kt, ft, ft/min, degrees true, UTC epoch seconds**.
- `Observation.pos_time = now - seen_pos`. Positions older than 60 s are dropped.
- The **merge layer** keys on ICAO hex and keeps the freshest position. On ties a local receiver beats an aggregator. It also records `src` and `pos_source` (adsb, mlat, tisb, other) for confidence scoring.
- Failover: each source has a circuit breaker. After 3 failures or any 429, back off exponentially (max 5 min) and switch to the next source in the priority list. The poller never retries a 4xx at full speed, because adsb.fi counts 4xx toward IP blocks.

### 2.3 Efficient polling: query planner

Zone definitions are never sent to a source. A **query planner** turns active zones into a small set of circles.

1. **Reach.** For each active zone, compute the radius that captures anything that could arrive within the prediction horizon: `reach = zone.radius + horizon_min × 8 nm/min`. 8 nm/min is about 480 kt. With the default 10-minute horizon, that is roughly 85 nm for a 5 nm home zone.
2. **Two tiers per zone:**
   - **Inner circle** (`zone.radius + 25 nm`), polled **every 10 s**. These aircraft are near or arriving within ~3 min.
   - **Outer circle** (`reach`, capped at 250 nm), polled **every 30 s**. At 480 kt an aircraft moves 4 nm in 30 s, which is fine for 5–10 minute predictions.
   - **Adaptive:** the inner tier drops to 5 s while a candidate is under 2 min out. Both tiers slow to 60 s when nothing is in reach (e.g. 3 a.m.).
3. **Merging.** Overlapping circles from different zones are merged: if one circle contains another, poll only the larger one. Travel zones are only polled while active (inside their date window, or an ad-hoc zone before it expires).
4. **Budget.** A token bucket per source is set to **≤0.5 req/s** (half of adsb.fi's limit). One zone costs ~0.13 req/s (10 s inner + 30 s outer). Home plus two travel zones is ~0.4 req/s before merging, which is why circle merging and the idle slow-down matter.
5. **Bandwidth.** Measured at JFK (one of the busiest areas in the US) on 2026-10-14: an 80 nm circle held 284 aircraft, **32–38 KB gzipped**. At that density, home-zone polling (inner + outer tiers) is roughly 150–200 MB/day, much less in quieter places.

### 2.4 Enrichment sources (all cached locally)

| Need | Source | License / limits | How it's used |
|---|---|---|---|
| Type code, registration, description, year, owner, **military flag** | **tar1090-db** `aircraft.csv.gz` (wiedehopf/tar1090-db, `csv` branch, ~8 MB, ~622k rows; derived from the Mictronics readsb DB) | No license file in the repo. Treat it as personal-use and don't redistribute it in this repo. | Downloaded at startup and weekly into a SQLite table. Columns are `icao;reg;type;flags;desc;year;ownOp;`. **`flags` is a bit string: position 0 = military, 1 = interesting, 2 = PIA, 3 = LADD** (verified: US T-38s are `10`, FAA's N1 is `0001`). ~29k aircraft are flagged military. It fills gaps where the live source lacks `t`/`r`, and is the *only* enrichment for RTL-SDR and OpenSky data. |
| Airline / operator from callsign | **VRS standing-data** (`vradarserver/standing-data`) airlines + routes CSVs | **CC0** | ICAO 3-letter callsign prefix gives the operator. **`ownOp` is the registered owner, not the operator.** The live check showed an Endeavor CRJ-900 with `ownOp` = "BANK OF UTAH TRUSTEE". Operator rules match on airline (from callsign) *or* owner. |
| Origin / destination | 1) **VRS standing-data routes** (bulk CSV, local, CC0); 2) **adsbdb** `api.adsbdb.com/v0/callsign/{cs}` on a miss; 3) adsb.lol `routeset` (currently returning empty) | adsbdb has no published rate limit. Its route data is copyright of its compilers and **"may not be copied, published, or incorporated into other databases"**: cache it privately and never export it. | Cached per callsign for 24 h, misses for 6 h. **Every route gets a plausibility check:** the aircraft must lie within ~50 nm (plus a margin scaled by route length) of the origin→destination great circle, or be near origin or destination at low altitude. Callsign-based routes are often stale. In testing, adsbdb said DAL1061 was BOS→SFO while it was at 75 ft beside JFK. Implausible routes are shown greyed out and **ignored by the predictor**. |
| Airports, runways, elevations | **OurAirports** `airports.csv`, `runways.csv` (nightly) | **Public domain** | Used by the "descending toward a nearby airport" signal (§4.4) and runway headings for approach paths. Refreshed weekly. |
| Type designator to friendly name | `desc` field, or tar1090-db's ICAO type table | — | Used in notification text ("C-17 Globemaster III"). |

**Caching rules:** every lookup goes through one `cache` table with `(namespace, key, value_json, fetched_at, ttl_s, negative)`. Bulk datasets are imported into their own tables, swapped in by an atomic table rename, and stamped with a version so a failed download never leaves a half-imported table.

---

## 3. Data model (SQLite)

Persistent tables. Live tracks stay in memory.

```text
settings(key PK, value_json)                       -- poll intervals, horizon, quiet hours, notifier config, tile URL…
users(id PK, username, pw_hash, created_at)        -- one row; table exists so sessions have a FK
sessions(id PK, user_id, created_at, expires_at, user_agent)

zones(
  id PK, name, kind  CHECK(kind IN ('home','travel','adhoc')),
  lat, lon, elevation_ft, radius_nm,
  active_from, active_until,                       -- NULL = always (home); dates for travel; expiry for adhoc
  enabled, created_from  ('map','geolocation'), created_at
)

rules(
  id PK, name, enabled, priority ('normal','high','critical'),
  match_json,          -- see below; validated by pydantic
  trigger ('nearby','predicted','both'),
  zone_ids_json,       -- NULL = all active zones
  min_confidence, lead_min_s, lead_max_s,          -- NULL = use global defaults
  cooldown_s, bypass_quiet_hours, created_at, updated_at
)

alerts(                                            -- one row per (aircraft, rule, zone) episode
  id PK, dedup_key UNIQUE-while-open,              -- hex|zone_id|rule_id
  hex, callsign, reg, type_code, rule_id, zone_id,
  state ('predicted','nearby','departed','expired'),
  first_seen_at, predicted_at, entered_at, closest_at, ended_at,
  best_confidence, eta_at_alert_s, cpa_nm, cpa_alt_ft, cpa_bearing_deg,
  snapshot_json        -- enrichment + track excerpt at alert time (renders the history view)
)

notifications(id PK, alert_id FK, channel, sent_at, kind ('new','update','emergency'),
              payload_json, ok, error)

cooldowns(dedup_key PK, last_notified_at, last_state)    -- survives restarts; no re-alert spam after a reboot

aircraft_db(icao PK, reg, type_code, flags, descr, year, own_op, db_version)
airlines(icao PK, name, iata, callsign_tele, country)
airports(ident PK, icao, iata, name, lat, lon, elevation_ft, type)
runways(airport_ident, le_ident, he_ident, le_heading, he_heading, le_lat, le_lon, he_lat, he_lon)
routes(callsign PK, origin, destination, source, fetched_at)     -- standing-data import + adsbdb hits
cache(namespace, key, value_json, fetched_at, ttl_s, negative, PK(namespace,key))

push_subscriptions(id PK, endpoint, keys_json, created_at)       -- phase 5, if web push is used
source_status(name PK, last_ok_at, last_error, consecutive_failures, requests_today)
```

**Rule `match_json`.** Conditions are AND-ed together, and values inside a list are OR-ed. Every field is optional.

```json
{
  "type_codes": ["C17", "B74*", "A388"],     // glob on ICAO type designator
  "registrations": ["N628TS"],
  "operators": ["UAL", "Atlas*"],            // airline ICAO prefix OR owner/operator substring
  "military": true,
  "callsign_regex": "^(RCH|REACH)\\d+",
  "squawks": ["7500", "7600", "7700"],
  "categories": ["A7"],                      // emitter category, e.g. rotorcraft
  "alt_ft":   {"min": null, "max": 10000},
  "max_distance_nm": 3,                      // tighter than the zone radius
  "min_elevation_deg": 15                    // "high enough above the horizon to see"
}
```

A built-in "Emergency squawk" rule (7500/7600/7700, priority `critical`, bypasses quiet hours) ships enabled. It requires the squawk to be seen in **2 consecutive updates**, because single-frame squawk glitches are common.

**In-memory track (per hex):** a ring buffer of the last ~40 positions (≈3–6 min), deduplicated on `pos_time`. It also holds the latest enrichment, derived kinematics (smoothed track, turn rate, along-track accel, vertical-rate trend), and the per-zone prediction result. ~4 MB for 1,000 aircraft.

**Recordings (for testing, §4.6):** written to `/data/recordings/YYYY-MM-DD/HH.ndjson.gz`, one compact line per observation. Retention is 14 days, rolling. Pinned scenarios move to `/data/recordings/pinned/`. About 50–100 MB/day for a busy home zone.

---

## 4. Prediction approach

Goal: alert **"likely to pass within your zone in N minutes, and you can see it"** with enough lead time to step outside. Turning, orbiting and maneuvering aircraft must not cause false alarms.

### 4.1 Geometry

- Prediction runs in a **local tangent plane (ENU) centered on each zone**. The equirectangular error inside 250 nm is under ~1%, far below the prediction uncertainty. Displayed distances and bearings use haversine and initial bearing.
- **Closest point of approach** on a straight segment: with `p` = relative position and `v` = velocity, `t* = clamp(-p·v / |v|², 0, H)` and `d_cpa = |p + v·t*|`.
- **Zone entry ETA** is the smallest root of `|p + v t| = R`, from a quadratic.
- Curved paths are sampled at 10 s steps and checked segment by segment.
- **Visibility.** The elevation angle from the zone center at CPA is `atan2(alt_ft − zone.elevation_ft, d_cpa_ft)`. The bearing at CPA becomes "passing 1.2 nm NW, ~45° up".

### 4.2 Kinematic state per aircraft

Built from the ring buffer on every update:

- **Track and speed:** the reported `track` and `gs`, cross-checked against positions over the last 30–60 s. On disagreement, positions win, because MLAT/TIS-B velocities are noisy.
- **Turn rate:** a robust slope of unwrapped track over the last ~60 s. Its variability (stddev of turn rate over ~2 min) is the main "is it maneuvering?" signal.
- **Autopilot intent (high-value signal).** Many ADS-B v2 aircraft broadcast `nav_heading` and `nav_altitude_mcp`. The live sample had them on airliners.
  - If `nav_heading` differs from the current track by more than 5°, the aircraft **is about to turn to that heading**. Project a standard-rate turn (3°/s, or 1.5°/s above FL250) onto it, then fly straight.
  - `nav_altitude_mcp` caps the vertical projection: the climb or descent levels off at the selected altitude.
- **Vertical:** prefer `geom_rate`, fall back to `baro_rate`. Projected altitude is clamped at the selected altitude and at ground/field elevation.

### 4.3 Two-stage prediction

**Stage 1 — cheap gate (every aircraft, every update).** A straight-line projection plus the `nav_heading` turn, over horizon `H` (default 10 min). An aircraft becomes a *candidate* for a zone if `d_cpa < 3·R` and the entry ETA is within `H`. This rejects ~95% of traffic for almost no CPU.

**Stage 2 — ensemble (candidates only).** Sample **K = 64 trajectories** per candidate with numpy:

- initial track ~ N(track, σ_trk), where σ_trk depends on position source and accuracy fields (`nac_v`, MLAT)
- ground speed ~ N(gs, σ_gs)
- turn rate = observed turn rate decaying to 0 over ~60 s, plus random turn rate N(0, σ_turn)
  - σ_turn scales with recent maneuvering and phase of flight: low and slow is more maneuverable than cruise
  - ~10% of samples follow the `nav_heading` turn when one is present
- vertical rate decays toward 0 as altitude reaches `nav_altitude_mcp`

Outputs:

- `p_enter`: the fraction of samples that enter the zone **and** satisfy the rule's altitude and elevation-angle constraints within `H`.
- `eta_p10 / p50 / p90`, shown as "in 4–6 min".
- `cpa_nm`, `cpa_alt_ft`, `cpa_bearing`, `elevation_deg` from the median trajectory.
- A **predicted-path polyline** (median, plus a p10–p90 band) for the map.

Cost: ~20 candidates × 64 samples × 60 steps per update, which is trivial in numpy.

### 4.4 Confidence score

```text
confidence = p_enter
           × q_data         # 0.6–1.0: pos age, MLAT/TIS-B penalty, NIC/NAC, track-history length (<30 s ⇒ capped at 0.5)
           × q_horizon      # exp(−ETA/τ), τ ≈ 12 min cruise, ≈ 5 min below 5,000 ft (GA / helis wander)
           × q_corroborate  # 0.7–1.3, see below; result clipped to [0,1]
```

**Corroboration** (Phase 4) can raise or lower the score:

- **Known destination (plausible route only).** If the great circle to the destination, or the standard arrival geometry, passes through the zone, multiply by 1.2. If the destination lies in the opposite direction, multiply by 0.8.
- **Descent toward a nearby airport.** The aircraft is descending (< −500 fpm), below ~12,000 ft, and heading within ±30° of an airport within ~40 nm. Run a second projection along the extended runway centerline of the runway best aligned with the arrival. If the zone lies under the approach path, multiply by 1.25; if the airport is reached before the zone, multiply by 0.8. This is the main way to catch "it will turn onto final over my house" cases that straight-line projection misses.
- **Climbing out from a nearby airport.** The mirror case for departures, using the runway heading.

**Persistence / hysteresis.** A predicted alert fires only when `confidence ≥ θ` (default 0.6, configurable per rule) on **2 consecutive evaluations**. Once fired, it isn't cancelled until confidence stays below θ − 0.15 for 3 evaluations. This stops flicker on aircraft hovering around the threshold.

### 4.5 Lead-time policy

- `lead_max` (default 10 min): don't alert earlier than this, because predictions are too uncertain.
- `lead_min` (default 90 s): if the predicted ETA is already shorter than this, it is effectively "nearby now" and goes through the nearby path.
- The alert fires at the **first** evaluation where `ETA_p50 ≤ lead_max` and the confidence and persistence conditions hold. For a typical airliner at 250–300 kt on descent, that gives ~4–8 minutes of warning.

### 4.6 Testing against recorded flight data

1. **Unit tests on synthetic tracks**, generated in code and deterministic:
   - straight overflight, near miss at 1.05·R, constant-rate turn into or away from the zone
   - 360° orbit (police or news helicopter), holding pattern
   - ILS approach with a turn onto final over the zone, go-around
   - stale and MLAT-jittered positions, squawk glitch

   Each asserts alert/no-alert and an ETA within tolerance.
2. **Recorder.** On from Phase 1, so real data from *your* area builds up before the predictor exists. It stores normalized observations as hourly NDJSON.gz files (§3).
3. **Replay harness** (`python -m app.replay`):
   - Streams a recording through the real pipeline with an **injected clock**. No wall-clock calls anywhere in the engine.
   - Ground truth comes from the future of the same recording: did the aircraft actually enter the zone (meeting the rule's altitude and elevation constraints), and when?
   - Reports:
     - **Precision and recall** of predicted alerts per zone/rule
     - **False alarms per day**
     - **Lead time** actually achieved (distribution)
     - **ETA error** (p50/p90)
     - **Calibration** (reliability curve and Brier score: do 70% predictions come true ~70% of the time?)
   - Supports parameter sweeps (θ, σ_turn, τ, H). Tune on one set of days and validate on different days to avoid overfitting.
4. **Regression fixtures.** A few curated, small (<1 MB) replay scenarios live in `tests/fixtures/replay/`, each with an expected-alerts file, and run in CI. These are short excerpts of ODbL / adsb.fi-sourced data used for personal testing. The repo should stay private, or the fixtures should be swapped for synthetic or own-receiver data before publishing.
5. **Optional bulk history.** adsb.lol published full daily dumps on GitHub (`adsblol/globe_history_2025`, archived Jan 2026; whether a 2026 successor exists still needs checking). They are useful for testing zones other than home without waiting weeks for recordings.

**Initial acceptance targets**, revisited after the first baseline: on replayed data for the home zone,

- recall ≥ 90% for aircraft that fly a steady track into the zone, with ≥ 3 min median lead
- **≤ 1 false predicted alert per zone per day** at default settings
- calibration error ≤ 0.1

---

## 5. Notifications

### 5.1 Options evaluated

| | **Home Assistant Companion app** (`notify.mobile_app_<phone>` via HA REST API) | **Self-hosted ntfy** | **Web Push from the PWA** (VAPID) |
|---|---|---|---|
| New infrastructure | None. Uses your HA and a long-lived access token. | One more container (~20 MB RAM) and the ntfy app | None (built into the app); needs HTTPS |
| Delivery when you're away from home | **Yes, without exposing anything.** Pushes go through HA's push relay to FCM/APNs. | iOS: your server pokes ntfy.sh (`upstream-base-url`, topic + message ID only), then the app **fetches the message from your server**, so your server must be reachable from the phone. Android: instant delivery holds a connection to your server (same requirement) unless it uses ntfy.sh's FCM. | Yes, via the browser vendor's push service. iOS 16.4+ only for an **installed** PWA. Reliability on iOS is weaker. |
| Limits | **500 pushes per device per day** (resets at midnight UTC). Critical alerts don't count. | None self-hosted (ntfy.sh upstream has its own limits for the iOS poke) | Vendor-side, generous |
| Rich features | `tag` (**replace the same notification in place**, e.g. update the ETA instead of stacking), `group`, Android channels, iOS **critical alerts** (bypass silent mode), actionable buttons, image, `url` to open the PWA | Priority 1–5, tags/emoji, click URL, action buttons, attachments | Title, body, icon, click URL; limited actions on iOS |
| Extras | Can also trigger HA automations (e.g. flash a light when a C-17 is 2 min out) | Works without HA; nice desktop notifications | No other app needed |

### 5.2 Recommendation

**Home Assistant notify as the primary channel**, behind a `Notifier` interface. It needs no new container. It works off-network without exposing the server. Its `tag` field fits the de-duplication model exactly. Critical alerts are a good match for emergency squawks.

**ntfy is the secondary plug-in** (Phase 2). Use it if you'd rather not depend on HA for this, or want desktop notifications.

**Web Push** is Phase 5 and optional.

### 5.3 Message content

```text
Title: ✈ C-17A Globemaster III · in ~4 min
Body:  RCH452 (USAF) will pass 1.2 nm NW of Home at ~3,500 ft, about 45° up, heading SE.
       Now 18 nm NW · 280 kt · descending · confidence 82%
       Rule: Military
Tap  → opens PWA map focused on that aircraft
```

The "nearby now" variant leads with distance and direction. The emergency variant leads with the squawk meaning: "7700 general emergency".

### 5.4 Cooldowns and de-duplication

- **Dedup key** = `hex | zone | rule`. One open `alerts` row per key acts as a state machine: `predicted → nearby → departed`, or `predicted → expired` if the aircraft turns away.
- **One notification per episode** by default.
  - Moving to `nearby` updates the *same* notification via HA `tag` = dedup key, so it doesn't stack a new one. This update can be turned off per rule.
  - An ETA change of more than 2 min also updates in place.
- **Cooldown:** after an episode ends, the same key can't notify again for `cooldown_s` (default 30 min). It is stored in `cooldowns`, so a container restart doesn't re-alert.
- **Multi-rule collapse:** if one aircraft matches several rules, send one notification listing all of them, at the highest priority.
- **Global rate cap:** at most 6 notifications per 10 min. Beyond that, send a digest ("+4 more watched aircraft nearby"). This keeps well under HA's 500/day.
- **Quiet hours** (optional) suppress everything except rules with `bypass_quiet_hours`, which emergency squawks have by default.

---

## 6. Web UI

Svelte SPA, installable as a PWA, responsive from phone to desktop.

1. **Map** (default screen)
   - Zones as circles; aircraft icons rotated by track and colored by status (matching / predicted / other).
   - **Predicted paths:** median polyline plus a translucent uncertainty band, opacity tied to confidence.
   - Tapping an aircraft shows a details sheet: type, registration, operator, route (with a plausibility badge), altitude, speed, ETA, CPA, confidence, matched rules.
   - Live via SSE. Filter toggle: "matching only / all in zone".
2. **Alerts**: a filterable history (rule, zone, date, type). Each entry shows a snapshot mini-map of the track at alert time and the notifications sent.
3. **Zones**: list plus a map editor (drag the center, radius slider).
   - Home zone, plus travel zones with a date range.
   - **"Use my location"**: browser geolocation creates an ad-hoc zone with an expiry (default 24 h), one tap.
4. **Rules**: form builder for every match field.
   - **"Test now"** lists which aircraft currently in reach would match, so you can see a rule work without waiting.
   - Enable/disable toggle, priority, and per-rule threshold and lead-time overrides.
5. **Settings**
   - Source status: requests/day, last error, circuit-breaker state.
   - Notifier config with a **"Send test notification"** button.
   - Quiet hours, horizon and defaults.
   - DB versions with "refresh now".
   - Required attribution: adsb.fi link, ODbL notice for adsb.lol, OSM tiles.

---

## 7. Phased build order

Each phase ends with something usable, tests passing, and a working container.

### Phase 0 — Skeleton (small)
- Repo layout, `pyproject.toml` (uv), ruff/mypy/pytest, Svelte+Vite scaffold, Dockerfile (multi-stage, non-root), `compose.yaml` with `mem_limit` and a healthcheck, `/data` volume.
- SQLite with migrations, config via env plus a settings table.
- Built-in auth (login page, session cookie, first-run password setup).
- CI (GitHub Actions): lint, typecheck and tests for both backend and frontend, plus a Docker build.

### Phase 1 — MVP: "watched type nearby" alerts ⭐
- adsb.fi source, adsb.lol failover, normalization, circuit breakers, token-bucket rate limiting.
- Query planner for the **home zone only**, inner tier only.
- tar1090-db import, enriching type, registration, military flag and owner.
- Rule engine for `type_codes`, `registrations`, `military`, `alt_ft`, `max_distance_nm`. Trigger: **nearby**.
- Alert state machine, dedup, cooldown persisted across restarts.
- **Home Assistant notifier** with `tag`-based updates and a test button.
- Minimal UI: login, rules list/editor (those fields), alert history list, a simple aircraft table.
- **Recorder turned on**, so replay data starts accumulating now.
- ✅ Done when: a matching aircraft within N nm triggers exactly one phone notification with type, distance, direction, altitude, and doesn't repeat during the cooldown.

### Phase 2 — Full zones, rules, map, PWA
- Travel zones with date windows, ad-hoc "use my location" zones, multi-zone planning with circle merging.
- All rule fields: operators via airlines/callsign, callsign regex, squawks with 2-update persistence, categories, elevation angle. Built-in emergency rule. Quiet hours, multi-rule collapse, global rate cap.
- Map view with SSE, aircraft details sheet, alert history with snapshots.
- PWA manifest, icons, service worker.
- ntfy notifier plug-in.

### Phase 3 — Prediction core
- Track ring buffers and derived kinematics (turn rate, `nav_heading`/`nav_altitude_mcp` intent).
- Two-tier polling (outer circle), Stage 1 gate, Stage 2 ensemble, confidence score (without corroboration), hysteresis, lead-time policy.
- `predicted` trigger in the rule engine; predicted paths on the map; ETA and confidence in notifications.
- **Synthetic scenario tests, replay harness, metrics report.** Tune defaults on the Phase 1–2 recordings and commit the baseline metrics.

### Phase 4 — Route and airport signals
- Route lookups: standing-data bulk import, adsbdb fallback, plausibility check, cache. Route display in the UI.
- OurAirports and runways import; descent-to-airport and departure projections; corroboration factor.
- Re-run the replay metrics and keep the signals only if they improve recall without raising false alarms.

### Phase 5 — Extras
- Local RTL-SDR source (readsb `aircraft.json`), with merge priority over aggregators.
- Optional OpenSky source (OAuth2, credit-aware scheduling).
- Optional Web Push; HA automation hooks; backup/export of rules and zones; retention jobs.

---

## 8. Proposed repository layout

```text
backend/
  app/
    main.py                # FastAPI app + lifespan (starts poller / evaluator tasks)
    config.py  clock.py    # settings; injectable clock (real / replay)
    auth.py
    db/        (schema.sql, migrations/, repo.py)
    sources/   (base.py, adsbfi.py, adsblol.py, opensky.py, readsb.py)
    ingest/    (planner.py, poller.py, ratelimit.py, normalize.py, merge.py, recorder.py)
    enrich/    (aircraft_db.py, airlines.py, routes.py, airports.py, cache.py)
    tracks/    (store.py, kinematics.py)
    predict/   (geo.py, cpa.py, ensemble.py, confidence.py, airports_signal.py)
    rules/     (models.py, engine.py)
    alerts/    (manager.py, format.py)
    notify/    (base.py, homeassistant.py, ntfy.py, webpush.py)
    api/       (zones.py, rules.py, alerts.py, live_sse.py, settings.py)
    replay/    (__main__.py, groundtruth.py, metrics.py)
  tests/  (unit/, scenarios/, fixtures/replay/)
frontend/  (Svelte + Vite; src/routes: Map, Alerts, Zones, Rules, Settings)
docker/Dockerfile
compose.yaml
docs/PLAN.md
CLAUDE.md
```

---

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| A free API changes terms, adds keys or blocks us (airplanes.live already did) | Pluggable sources with failover. Polite polling at ≤50% of published limits. Feeding a receiver is the long-term fix. |
| Coverage gaps at low altitude near home | Phase 5 RTL-SDR source. The UI shows the position source so gaps are visible. |
| Wrong routes from callsign databases | Plausibility check; implausible routes never influence predictions. |
| False alarms from maneuvering aircraft | Ensemble probability, maneuver-scaled σ_turn, persistence/hysteresis, metrics-driven tuning on replay data. |
| Notification spam | Episode state machine, `tag`-based updates, cooldowns persisted across restarts, global rate cap, digest. |
| Resource creep on a shared server | Single process, SQLite, `mem_limit`, adaptive poll slow-down, bounded ring buffers and retention. |

---

## 10. Open questions (please answer before approval)

See the summary in the chat. The same questions are tracked here:

1. Phone OS (iOS / Android)? Is the HA Companion app installed and signed in?
2. How is the server reachable from outside: reverse proxy (which), Tailscale/WireGuard, Cloudflare Tunnel, or LAN only? Do you already use SSO such as Authelia or Authentik?
3. Server CPU architecture (x86-64 / ARM64) and roughly how much RAM is free?
4. Backend language: Python (recommended) or Go?
5. Rough home region (city level is fine) and home-zone radius. This tunes reach, poll tiers and the realistic coverage expectation.
6. Default lead time: is ~5–10 min right, and should a minimum elevation angle (e.g. 15°) apply by default?
7. Will you feed an RTL-SDR to adsb.fi / adsb.lol (better access, future-proof), or only use it locally?
8. Repo visibility: will this repo stay private? This affects whether replay fixtures from aggregator data can be committed.
