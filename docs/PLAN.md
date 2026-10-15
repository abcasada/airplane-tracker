# Airplane Tracker — Build Plan (v2)

Status: **draft v2, awaiting approval**. Nothing in here is built yet.
Research and live API checks were done on 2026-10-14/15. Re-check terms before each phase that adds a new source.

Personal ADS-B alert service. It runs in one Docker container on a home server and has a PWA web UI reachable at your own domain. It alerts on aircraft that match user-defined rules:

- aircraft **visible from where you are**, now or soon
- aircraft **headed to an airport** you care about
- aircraft **headed into an area** you care about (e.g. a bombing range)

…with a strong focus on **military** aircraft.

### What changed since v1 (from your answers)

- **No RTL-SDR.** RTL-SDR is a ~$30–60 USB radio receiver plus antenna that hears aircraft directly. It isn't needed. Data comes from free public APIs, with optional **paid** add-ons (§3.2).
- **"Zones" are now "places" in two roles** (§4):
  - *viewpoints*: where you are (home, or your phone's location when traveling)
  - *targets*: airports by ICAO code, and areas such as the Smoky Hill range as an official polygon
- **Claimed destination** is a first-class feature: free callsign route databases, plus optional FlightAware filed flight plans (§6.1). **Inferred arrival** comes from flight behaviour (§6.2).
- **Military classifier** modelled on FR24's military filter. It combines database flags, military address blocks, type, callsign, registration and operator (§5.2).
- **Android + Home Assistant** notification design (§9). Public access via **your domain** with hardened auth (§2.1).
- **Tuning simulator** (§10): replay the last days of traffic with different settings and see how many alerts you *would* have received.
- **Public-vs-private repo** recommendation (§12).

---

## 1. Decisions so far

| Topic | Decision |
|---|---|
| Phone / notifications | Android, **Home Assistant Companion app** (already installed). HA is the primary channel. |
| Access | Public HTTPS at a subdomain of your domain, e.g. `adsb.adamcasada.com` (§2.1) |
| Server | x86-64, RAM not a constraint. The app still stays small; it shares the box. |
| Language | Python |
| Home region | Salina, KS. Initial targets: **KSLN** (Salina Regional) and the **Smoky Hill ANG Range** (R-3601A/B). Travel targets added ad hoc. |
| Lead time / visibility | Fully configurable (global → per target → per rule). By default, skip anything too low on the horizon to see. |
| Receiver | None. Public APIs only; paid sources allowed. |

---

## 2. Recommended stack

The design goal is **one small container, one process, no extra databases**.

| Layer | Choice | Why |
|---|---|---|
| Backend | **Python 3.12, FastAPI, uvicorn (single worker), asyncio** | One event loop runs the pollers, the evaluation loop and the HTTP API. |
| HTTP client | `httpx` (async, gzip) | Both free sources serve gzip; see §3.3 for sizes. |
| Storage | **SQLite** (WAL) via `aiosqlite`, numbered SQL migrations | No DB server. It holds config, places, rules, alert history, caches and the imported aircraft DB (~622k rows). |
| Live state | In-process memory (per-aircraft ring buffers) | Rebuilt within one poll after a restart. Alert and cooldown state persist in SQLite. |
| Numerics | `numpy` (prediction ensemble) and `shapely` (polygon targets) | Both are small and well understood. |
| Validation | `pydantic` v2 | One schema for API, engine and DB. |
| Auth | Built-in single-user login with **TOTP 2FA** (argon2id, `pyotp`) | It's exposed to the internet (§2.1). |
| Frontend | **Svelte + Vite** SPA, served by FastAPI as static files | Small bundle, good for the form-heavy editors. |
| Map | **Leaflet** with OSM raster tiles (tile URL configurable) | Light. |
| Live updates | Server-Sent Events | Works through any reverse proxy or tunnel. |
| PWA | Manifest + small service worker (app shell only) | Android Chrome installs it as an app. |
| Tooling | `uv`, `ruff`, `mypy --strict`, `pytest`, `respx`; `npm`, `svelte-check` | |
| Container | Multi-stage build, `python:3.12-slim`, non-root, `/data` volume, healthcheck | Targets: under 200 MB image, under 150 MB RSS, under 2% CPU average. |

### 2.1 Reaching it at your domain, and auth

The app speaks plain HTTP inside Docker. Something in front of it terminates TLS for `adsb.<your-domain>`. Two good options:

| | **Cloudflare Tunnel** (`cloudflared` container) | **Reverse proxy + port 443 forwarded** (Caddy, Traefik or Nginx Proxy Manager) |
|---|---|---|
| Router changes | None. No open ports, home IP hidden. | Forward 443 (and 80 for certificates); dynamic DNS if your IP changes |
| TLS | Cloudflare | Let's Encrypt (automatic in Caddy/Traefik/NPM) |
| Needs | Domain DNS hosted on Cloudflare (free plan) | Any DNS |
| Notes | Don't put Cloudflare Access login in front; it breaks PWA background fetches. The app's own login is used. | Reuse it if you already run one for other containers. |

**Recommendation:** use whichever you already have for other services. If you have neither, use **Cloudflare Tunnel**: it's the least exposure for a home server.

**Auth hardening, because it's internet-facing:**

- Single user. Password plus **TOTP** (any authenticator app), with 10 one-time recovery codes.
- 30-day sliding session cookie (`HttpOnly`, `Secure`, `SameSite=Lax`), so the PWA rarely asks you to log in.
- Login lockout with exponential backoff per IP and per account. The real client IP is read only from trusted proxy headers.
- CSRF protection: Origin check plus token on state-changing requests. Strict security headers (CSP, HSTS, frame-deny). OpenAPI `/docs` is disabled in production.
- Audit log of logins and setting changes.
- Optional `AUTH_MODE=proxy` remains for anyone who runs forward-auth.
- Notification payloads and anything else that leaves the server carry **no home coordinates**.

---

## 3. Data sources

### 3.1 Free live position sources (checked 2026-10-14/15)

| Source | Access | Rate limit | Terms | Live check | Verdict |
|---|---|---|---|---|---|
| **adsb.fi Open Data** — `opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{nm}` (≤250 nm) | No key | **1 req/s**. 400/401/403/404/429 count toward a **temporary IP block**. | Personal, **non-commercial**; **must cite and link adsb.fi**; may cut off at any time | 200 OK. Has `r`, `t`, `desc`, `ownOp`, `year`, **`dbFlags`** (1 = military), `nav_heading`, `nav_altitude_mcp`, vertical rates, `squawk`, `emergency`. | **Primary** |
| **adsb.lol** — `api.adsb.lol/v2/point/{lat}/{lon}/{nm}` (≤250 nm) | No key today. A key ("get it by feeding") is planned. | Unpublished, "dynamic" | Data is **ODbL**; asks you to contact them for production use | 200 OK, same JSON shape. `routeset` returned 201 with an empty body. | **Failover** |
| airplanes.live | — | — | — | **403** "Please contact us…"; API docs repo archived Apr 2026 | Excluded |
| OpenSky Network | OAuth2 client credentials | 4,000 credits/day registered (1 credit per ≤25 sq° box) | Non-commercial | not polled | Optional fallback. No type or registration. |

**Coverage around Salina (live check 2026-10-15, 80 nm radius around KSLN):**

- 34 aircraft: mostly airliners in cruise, plus bizjets, a FedEx feeder Caravan at 6,900 ft, and regional jets below 3,000 ft near other airports.
- Also **`SAM491` (98-0001, C-32A)** at FL350, flagged military via `dbFlags=1`. That confirms the military flag comes through the API.
- Low-altitude coverage *at KSLN itself* (pattern altitude, ~1,300 ft field elevation) is unknown until we record a few days. Phase 1 includes a **coverage report** showing the lowest altitude seen by distance from KSLN. If it turns out poor, the remedy is a receiver (optional, ~$60) or a paid source.
- Military fighters frequently fly with ADS-B off. Expect to see bombers, tankers, transports and helicopters more often than fighters on the range.

### 3.2 Paid options (optional, all behind the same plug-in interfaces)

| Service | Price | What it adds | Recommendation |
|---|---|---|---|
| **FlightAware AeroAPI — Personal** | No minimum. **First $5/month of usage free**. `/flights/{ident}` costs $0.005 per result set. 10 result sets/min. Card required. Personal/academic use allowed. | **Filed flight-plan origin and destination**, including GA/bizjet IFR flights that callsign databases don't know | **Yes, as the "claimed destination" upgrade** (Phase 2). Only looked up for aircraft that matter (rule candidates near your targets), cached per flight. A **monthly budget cap** (default $5, so it stays free) is enforced by the app. Roughly 1,000 lookups/month, ~30/day. |
| Flightradar24 API — Explorer | $9/month, 30,000 credits, 10 queries/min | Live positions with origin/destination from FR24's network | Not now. Overlaps with the free feeds plus AeroAPI. Per-endpoint credit costs and personal-use terms still need checking. Possible later as a paid *position* source if the free APIs ever close. |
| ADS-B Exchange (RapidAPI) | Paid | Same kind of unfiltered data as adsb.fi | No. Redundant. |

### 3.3 Pluggable sources and efficient polling

- Every source implements one `Source` protocol and normalizes into one `Observation` type. Units: nm, kt, ft, ft/min, degrees true, UTC epoch seconds.
- A merge layer keys on ICAO hex and keeps the freshest position. Each source has a circuit breaker, with failover from adsb.fi to adsb.lol. **Any 4xx/429 backs off exponentially** (adsb.fi IP-blocks on repeated 4xx).
- **Query planner:** never pulls global feeds.
  - Each active viewpoint and target gets an **inner circle**: its own radius + 25 nm, polled every 10 s, dropping to 5 s while something is under 2 min out.
  - It also gets an **outer circle**: radius + horizon × 8 nm/min, ≈ 85 nm for a 10-min horizon, polled every 30 s.
  - Everything slows to 60 s when nothing is in reach.
  - Overlapping circles are merged. **Home, KSLN and Smoky Hill are all within ~15 nm of each other, so they collapse into one inner and one outer query.**
- Budget: ≤0.5 req/s per source (half of adsb.fi's limit). The Salina setup uses about **0.13 req/s**.
- Bandwidth: an 80 nm circle at JFK (very busy) was 32–38 KB gzipped. Around Salina (34 aircraft) it's a few KB per poll, so **tens of MB/day**.

---

## 4. Places: viewpoints and targets

| Kind | Defined by | Used for |
|---|---|---|
| **Viewpoint** | Point + elevation (auto from lat/lon), visibility settings | "Can I see it from here?" Home is permanent. Travel viewpoints have date windows. **"Use my location"** creates a viewpoint from the phone's GPS with an expiry (default 24 h). |
| **Airport target** | ICAO code (e.g. `KSLN`). Name, position, elevation and **runways** auto-filled from OurAirports. | "Is it headed to / arriving at this airport?" Claimed destination plus inferred arrival (§6). |
| **Area target** | **Polygon** (drawn on map, pasted coordinates, or a built-in FAA special-use-airspace lookup in Phase 4) or a circle, plus an optional altitude band | "Is it headed into, or operating in, this area?" |

**Salina starting set.** This is your configuration, entered through the UI or a setup wizard and stored in `/data`, never in the repo:

- **Home viewpoint.** Your house; exact coordinates are entered only in the app.
- **KSLN**, Salina Regional: 38.7910 N, 97.6522 W, elevation 1,288 ft. Runways 17/35 (12,301 ft, which is why heavy military aircraft practice here), 12/30 (6,510 ft), 04/22, 18/36. It's ~4 nm SW of downtown Salina, so arrivals are generally visible from town.
- **Smoky Hill ANG Range — R-3601A/B (Brookville, KS).** Polygon from the Federal Register (80 FR 60528, 2015):

  | Vertex | Latitude | Longitude |
  |---|---|---|
  | 1 | 38°45′20″N | 97°46′01″W |
  | 2 | 38°39′45″N | 97°46′01″W |
  | 3 | 38°38′20″N | 97°47′31″W |
  | 4 | 38°38′20″N | 97°50′01″W |
  | 5 | 38°35′00″N | 97°50′01″W |
  | 6 | 38°35′00″N | 97°56′01″W |
  | 7 | 38°45′20″N | 97°56′01″W |

  - Altitudes: R-3601A is surface to FL180; R-3601B is FL180–FL230.
  - Active Mon–Sat 0900–1700 local, other times by NOTAM.
  - The range is ~12 nm SW of KSLN. The surrounding Smoky / Smoky High MOAs are larger and can be added as a second, wider area target.
  - To verify against current FAA data before use, because the range has an expansion project.
- **Nearby, optional later:** McConnell AFB (KIAB, 72 nm, KC-46 tankers), Marshall AAF / Fort Riley (KFRI, 44 nm).

---

## 5. Enrichment and military classification

### 5.1 Enrichment sources (all cached locally)

| Need | Source | License / limits | Notes |
|---|---|---|---|
| Type, registration, description, year, owner, **military flag** | **tar1090-db** `aircraft.csv.gz` (~8 MB, ~622k rows) + `ranges.json` | No license file; personal use, never redistributed | Columns `icao;reg;type;flags;desc;year;ownOp`. `flags` positions: 0 = military, 1 = interesting, 2 = PIA, 3 = LADD. ~29k military airframes. `ranges.json` lists **32 military ICAO address blocks**, e.g. US `adf7c8–afffff`, UK `43c000–43cfff`. Refreshed weekly. |
| Operator from callsign | VRS standing-data airlines | CC0 | `ownOp` is the registered *owner*. Two live examples: "BANK OF UTAH TRUSTEE" on a United 737 MAX; "WILMINGTON TRUST CO TRUSTEE" on a United A321neo. |
| Routes by callsign | VRS standing-data routes (bulk, CC0) → adsbdb on a miss → adsb.lol routeset | adsbdb route data must not be redistributed | Plausibility-checked (§6.1) |
| Filed flight plan | **FlightAware AeroAPI** (optional, paid, capped) | Personal use | §3.2 |
| Airports, runways, elevations | OurAirports | Public domain | Airport targets and approach geometry |
| Terrain elevation for viewpoints | A free elevation API (e.g. USGS Elevation Point Query Service) when a viewpoint is created | Public | One lookup per viewpoint, cached; manual entry if the lookup fails |

### 5.2 Military classifier ("FR24-style military filter, but explainable")

FR24's military filter isn't publicly documented. It appears to be driven by their own aircraft database (operator and category), and it only shows aircraft that broadcast and aren't withheld. We can match or beat it by **combining independent signals into a score with visible reasons**:

| Signal | Strength | Source / example |
|---|---|---|
| `dbFlags` military bit from the API, or the tar1090-db flag | Strong (0.9) | `SAM491` 98-0001 C-32A had `dbFlags=1` |
| ICAO address in a military allocation block | Strong (0.85) | tar1090-db `ranges.json` (US block `adf7c8–afffff`) |
| **Military-only type code** | Strong (0.8) | Derived automatically from tar1090-db: types where ≥95% of airframes are military-flagged (42 types, e.g. `C17`, `K35R`, `B52`, `B1`, `F35`, `F15`, `A10`, `C30J`, `V22`, `P8`, `E6`, `R135`, `C5M`, `E3TF`) |
| Dual-use type code | Weak (0.2–0.5, scaled by military share) | Same derivation, 15–95% military. E.g. `H60` 86%, `C130` 90%, `F16` 89%, `T38` 93%, `TEX2` 94%, `BE40` 20% (T-1A Jayhawk), `B350` 16% (C-12), **`B762` — the KC-46A shows up as a 767-200** |
| Callsign pattern | Medium (0.6) | Editable list: `RCH`/`REACH` (AMC airlift), `SAM`, `SPAR`, `EXEC`, `VENUS`, `PAT`, `CNV`, `NAVY`, … plus a *weak* "word + 2–3 digits with no airline match" heuristic for tactical callsigns (e.g. `HOOK21`) |
| Registration pattern | Medium (0.6) | US military serial `NN-NNNN(N)` (e.g. `98-0001`), UK `ZZ123`-style, other editable regexes |
| Owner/operator text | Strong (0.8) | "AIR FORCE", "NAVY", "ARMY", "MARINE", "NATIONAL GUARD", "COAST GUARD" |
| Government (separate class) | — | "FEDERAL AVIATION ADMINISTRATION", "CUSTOMS AND BORDER", "STATE OF …" → `government`, which FR24 lumps in with military. Toggleable in rules. |
| **Your overrides** | Final | Mark any hex or registration as military or not from the UI. Overrides always win. |

- Score = 1 − ∏(1 − wᵢ) over the signals that fire. Class `military` if ≥ 0.6 (configurable). Every alert shows *why*, e.g. "military: DB flag, US mil address block, callsign SAM".
- Rule fields support both `military: true` (uses the threshold) and the raw signals (`type_codes`, `callsign_regex`, `registrations`, `operators`), so you can build your own clue combinations.
- **Validation:** on recorded data, compare against the `dbFlags`-only baseline. The UI lists "possible military" (score 0.3–0.6) so you can review and override them, which grows your override list.

---

## 6. Destination: claimed and inferred

### 6.1 Claimed destination

Resolution order for each callsign/flight, cached:

1. **FlightAware filed flight plan** (if enabled and within budget). Most authoritative, and covers IFR GA/bizjets.
2. VRS standing-data routes (local, free).
3. adsbdb callsign API (free).

**Plausibility check (always applied).** The aircraft must be within ~50 nm (plus a margin scaled by route length) of the origin→destination great circle, or near the origin or destination at low altitude. Callsign databases are often stale: adsbdb listed DAL1061 as BOS→SFO while it was at 75 ft beside JFK.

Shown as:

- "→ KSLN (filed plan, FlightAware)"
- "→ KSLN (route DB)"
- "→ KSLN? (route DB, implausible)", ignored for alerting

Military and most VFR flights have **no** claimed destination. For those, inference is the only signal.

### 6.2 Inferred arrival (airport targets)

An **arrival score** per aircraft per airport target:

- within ~40 nm and below ~12,000 ft AGL
- descending, or level below 6,000 ft AGL; `nav_altitude_mcp` set below current altitude
- ground speed decreasing
- track within ±30° of the airport bearing, **or** aligned (±15°) with a runway's extended centerline within 15 nm (KSLN 17/35, 12/30, 04/22, 18/36)
- altitude roughly consistent with a 3° glide path or a pattern entry
- squawk 1200 low and close: VFR pattern traffic

**Approach projection.** For aircraft scoring high, the predictor projects a path **onto the most likely runway's final approach** instead of a straight line. This catches "it'll turn final over town" cases (§7).

**Practice approaches.** A low approach or touch-and-go followed by a climb-out is still a KSLN visit. It is labelled "practice approach", which is common for heavy military aircraft at KSLN.

**Local pattern traffic.** KSLN hosts the K-State Salina flight school, so there will be many training laps. A **pattern detector** tags an aircraft as `local_pattern` when it has stayed within 5 nm of the airport and below 3,000 ft AGL for more than 15 min with repeated turns. What to do with those is configurable: default *notify once per aircraft per day, then suppress*; or exclude by operator.

### 6.3 Area targets (e.g. the range)

- **Predicted entry:** the ensemble (§7) projects into the polygon and altitude band within the lead time.
- **Operating inside:** inside the polygon for more than N seconds, or orbiting/racetracking. Turn-rate history detects orbits.
- Range alerts don't require visibility from home by default: the range is ~15–20 nm from town, so most activity there is below 15° elevation. Rules can add visibility if you want.

---

## 7. Prediction approach

Goal: alert "will be **visible from your viewpoint** / **arrive at your airport** / **enter your area** in N minutes" with useful lead time. Turning and maneuvering aircraft must not cause false alarms.

### 7.1 Geometry

- Local tangent plane per place; the error is negligible within 250 nm.
- Straight-line CPA: `t* = clamp(-p·v/|v|², 0, H)`. Circle entry ETA comes from the quadratic. Curved paths are sampled at 10 s; polygon entry uses `shapely` on sampled segments.
- **Visibility from a viewpoint at time t:**
  - elevation angle = `atan2(alt_ft − viewpoint_elev_ft, ground_dist_ft)` ≥ `min_elevation_deg` (default **15°**, so things too low on the horizon are skipped), **and**
  - slant range ≤ a **size-based limit**: heavy/large 20 nm, medium 12 nm, light/rotorcraft 5 nm, from emitter category and type, all configurable.
  - Example: an airliner at FL350 passes the 15° test out to ~21 nm horizontal.
- Optional day/night flag from sun position, e.g. "night: lights only".

### 7.2 Kinematics per aircraft

From a ring buffer of the last ~40 positions:

- **Track, speed and turn rate:** smoothed track/speed; robust turn rate over ~60 s; turn-rate variability over ~2 min (the "maneuvering" signal).
- **Vertical:** `geom_rate`, falling back to `baro_rate`.
- **Autopilot intent:** if `nav_heading` differs from the current track by more than 5°, project a standard-rate turn onto it. `nav_altitude_mcp` caps the climb/descent.

### 7.3 Two-stage prediction

1. **Gate** (all aircraft, each update): straight line plus `nav_heading` turn over horizon `H` (default 10 min). Candidates are those passing within 3× the place's radius or approaching an airport/area target.
2. **Ensemble** (candidates only, numpy): K = 64 sampled trajectories.
   - track/speed noise scaled by data quality; turn rate = observed turn rate decaying to 0, plus maneuver-scaled noise
   - a share of samples follow the `nav_heading` turn and, for high arrival scores, the **runway approach path**
   - vertical rate levels off at the selected altitude

   Outputs:
   - `p_success` = fraction of samples that satisfy the target condition: visible from the viewpoint, landing at or low approach to the airport, or entering the area
   - `eta_p10/p50/p90`
   - CPA, elevation angle and direction from the median path
   - a predicted-path polyline with an uncertainty band for the map

### 7.4 Confidence

```text
confidence = p_success × q_data × q_horizon × q_corroborate   (clipped to [0, 1])
  q_data        0.6–1.0: position age, MLAT/TIS-B, NIC/NAC, history length (<30 s ⇒ cap 0.5)
  q_horizon     exp(−ETA/τ), τ ≈ 12 min cruise, ≈ 5 min below 5,000 ft AGL
  q_corroborate 0.7–1.3: claimed destination agrees (filed plan > route DB) or disagrees;
                arrival score; consistency over the last updates
```

**Hysteresis:** an alert fires at ≥ θ (default 0.6) on 2 consecutive evaluations, and is cancelled only after 3 evaluations below θ − 0.15.

### 7.5 Lead time (fully configurable)

Settings cascade **global → per place → per rule**:

- `lead_max` (default 10 min): never alert earlier than this.
- `lead_min` (default 90 s): if the ETA is shorter, it's "now" and goes through the nearby path.
- Alert at the first evaluation where `ETA_p50 ≤ lead_max` and the confidence and persistence conditions hold.
- The tuning simulator (§10) shows how changing these affects alert counts.

### 7.6 Testing against recorded flight data

1. **Synthetic scenario tests:**
   - straight overflight; near miss at 1.05·R; turn toward / away
   - orbit; holding pattern; KSLN straight-in and pattern entry; turn onto final over the viewpoint
   - practice approach + go-around; pattern laps (must tag `local_pattern`)
   - range racetrack inside the polygon; stale/MLAT jitter; squawk glitch
2. **Recorder** (on from Phase 1): compact hourly NDJSON.gz under `/data/recordings`, 14-day retention, pin-able scenarios.
3. **Replay harness** with an injected clock. Ground truth is taken from the recording's future (did it become visible / land / enter?). It reports:
   - precision and recall
   - false alarms per day
   - achieved lead time
   - ETA error
   - calibration (reliability curve, Brier score)
   - military-classifier agreement vs `dbFlags`

   Supports parameter sweeps; tune on some days and validate on others.
4. **Regression fixtures in CI:** synthetic, or short **adsb.lol (ODbL)** excerpts with attribution only. No adsb.fi, adsbdb or FlightAware data, so the repo can be public (§12).
5. **Initial targets** (reviewed after the first baseline):
   - ≥90% recall for steady tracks with ≥3 min median lead
   - ≤1 false predicted alert per place per day at defaults
   - calibration error ≤0.1

---

## 8. Data model (SQLite)

```text
settings(key PK, value_json)
users(id PK, username, pw_hash, totp_secret_enc, recovery_codes_hash_json, created_at)
sessions(id PK, user_id, created_at, expires_at, last_seen_at, ip, user_agent)
audit_log(id PK, at, actor, action, detail_json)

places(
  id PK, name, role CHECK(role IN ('viewpoint','airport','area')),
  lat, lon, elevation_ft,             -- area: centroid
  radius_nm,                          -- viewpoint/airport search radius; area: optional circle instead of polygon
  polygon_geojson,                    -- area targets
  alt_floor_ft, alt_ceiling_ft,       -- area altitude band (R-3601A: 0–17,999)
  airport_icao,                       -- airport targets
  visibility_json,                    -- viewpoint: min_elevation_deg, slant limits by size, night flag
  active_from, active_until,          -- NULL = always; travel dates; GPS viewpoints expire
  schedule_json,                      -- optional, e.g. range hours Mon–Sat 0900–1700
  lead_min_s, lead_max_s, min_confidence,   -- overrides
  enabled, source ('manual','geolocation','airport_lookup','faa_sua'), created_at
)
runways(airport_ident, le_ident, he_ident, le_heading, he_heading, le_lat, le_lon, he_lat, he_lon, length_ft)

rules(
  id PK, name, enabled, priority ('low','normal','high','critical'),
  match_json,          -- aircraft filters (below)
  condition_json,      -- which places and how: visible_from / arriving_at / entering / inside / near (distance)
  timing ('now','predicted','both'), min_confidence, lead_min_s, lead_max_s,
  notify_json,         -- HA channel, importance, cooldown_s, max_per_day, quiet-hours bypass, update-in-place, countdown
  created_at, updated_at
)

military_overrides(key PK,  -- 'hex:ae1234' or 'reg:98-0001'
                   is_military, note, created_at)
classifier_lists(name PK, entries_json)   -- callsign prefixes, registration regexes, operator keywords (editable)

alerts(id PK, dedup_key, hex, callsign, reg, type_code, rule_id, place_id,
       state ('predicted','active','departed','expired'),
       first_seen_at, predicted_at, entered_at, closest_at, ended_at,
       best_confidence, eta_at_alert_s, cpa_nm, cpa_alt_ft, cpa_bearing_deg, elevation_deg,
       destination_claimed, destination_source, arrival_score, military_score, military_reasons_json,
       snapshot_json)
notifications(id PK, alert_id, channel, sent_at, kind ('new','update','emergency','digest'),
              payload_json, ok, error)
cooldowns(dedup_key PK, last_notified_at, last_state, count_today)

aircraft_db(icao PK, reg, type_code, flags, descr, year, own_op, db_version)
mil_ranges(start_hex, end_hex)       -- from tar1090-db ranges.json
type_stats(type_code PK, airframes, military_share)   -- derived at import; drives the type signal
airlines(icao PK, name, iata, callsign_tele, country)
airports(ident PK, icao, iata, name, lat, lon, elevation_ft, type)
routes(callsign PK, origin, destination, source, fetched_at)
flight_plans(fa_flight_id PK, ident, origin, destination, filed_at, fetched_at)   -- AeroAPI cache
api_budget(source, month, spent_usd, calls)                                        -- paid-source cap
cache(namespace, key, value_json, fetched_at, ttl_s, negative, PK(namespace,key))
source_status(name PK, last_ok_at, last_error, consecutive_failures, requests_today)
```

**Rule example — "Military visible from home or arriving KSLN":**

```json
{
  "match":     {"military": true},
  "condition": {"any": [
                  {"visible_from": "home"},
                  {"arriving_at": "KSLN"},
                  {"entering": "Smoky Hill R-3601"}]},
  "timing": "both", "min_confidence": 0.5, "lead_max_s": 600,
  "notify": {"channel": "Aircraft – Military", "priority": "high", "cooldown_s": 3600, "countdown": true}
}
```

**Match fields:**

- `type_codes` (globs)
- `registrations`
- `operators`
- `military` / `government`
- `callsign_regex` (validated, length-limited)
- `squawks`
- `categories`
- `alt_ft` range
- `max_distance_nm`
- `exclude_local_pattern`
- `exclude` (any of the above)

**Built-in rule:** emergency squawks 7500/7600/7700 seen on 2 consecutive updates, critical priority, bypasses quiet hours.

---

## 9. Notifications (Android + Home Assistant)

HA's Companion app is the primary channel, driven by the app through HA's REST API with a long-lived token: `notify.mobile_app_<phone>`.

- Works off-network without exposing anything extra, because HA's push relay goes through FCM.
- 500 pushes/day/device cap. The app's own rate cap keeps it far below that.

**Android-specific features used:**

| Feature | Use |
|---|---|
| `channel` | One Android notification channel per rule group, e.g. "Aircraft – Military", "Aircraft – KSLN arrivals", "Aircraft – Emergency". **Sound, vibration and importance are then set per channel in Android settings.** This is a big part of "very configurable". |
| `importance: high`, `priority: high`, `ttl: 0` | Immediate delivery even in Doze |
| `tag` | **Update the same notification in place** (ETA changes, predicted → overhead) instead of stacking |
| `chronometer` + `when` | A live **countdown to ETA** in the notification shade |
| `timeout` | Auto-dismiss after the aircraft has passed |
| `clickAction` | Opens the PWA focused on that aircraft |
| `actions` | Buttons: "Snooze aircraft 2 h", "Mute rule today". Handled via HA's `mobile_app_notification_action` event, which the app receives over HA's WebSocket API. |
| `channel: alarm_stream` | Optional for emergency squawks: sounds even in Do Not Disturb |
| `notification_icon: mdi:airplane`, `color` | Military in a different color |

**Example:**

```text
✈ KC-135R · arriving KSLN · ~5 min   [countdown 04:59]
BOLT21 · USAF (military: DB flag, mil address block, type K35R)
Now 22 nm NE, 6,500 ft, descending, 240 kt → runway 35 approach likely (confidence 78%)
Visible from Home: passes 2.1 nm W at ~1,800 ft AGL, ~25° up
[Snooze 2 h]  [Mute rule today]
```

**De-duplication and cooldowns:**

- Dedup key is `hex | place | rule`, one open episode each.
- Several rules on one aircraft → **one** notification listing them.
- Per-rule cooldown (default 30 min; persisted across restarts) and `max_per_day`.
- Global cap of 6 per 10 min, then a digest ("+4 more").
- Quiet hours, with per-rule bypass.

**Secondary channels** behind the same `Notifier` interface:

- **Web Push** to the installed PWA. Reliable on Android Chrome, and needs no HA. Good backup if HA is down. Phase 5.
- **ntfy**: only if you ever want it.

---

## 10. Web UI

1. **Map**
   - Places: viewpoint circles, airport markers with runway lines, area polygons.
   - Aircraft colored by status (military / matching / predicted / other).
   - Predicted path plus uncertainty band.
   - Details sheet:
     - type, registration, operator
     - military score and reasons
     - claimed destination with source and plausibility
     - arrival score, ETA, CPA, elevation angle, matched rules

   Live via SSE.
2. **Alerts**: history with filters, snapshot mini-map, notifications sent. "Was this useful? 👍/👎" feedback, which the tuning report uses.
3. **Places**:
   - Home; add an airport by ICAO code; draw or paste an area polygon (later also FAA airspace search).
   - Travel viewpoints with dates; **"Use my location"**.
   - Visibility settings per viewpoint.
4. **Rules**: form builder for match + condition + timing + notify. **"Test now"** shows what currently in reach would match.
5. **Tuning**: the **what-if simulator**.
   - Pick a rule and settings: lead time, confidence, elevation angle, slant range, pattern-traffic handling.
   - Replay the last N days of recordings and see **how many notifications per day** you'd have gotten, and which ones.
   - This is how you fine-tune "not too many, not too few" without waiting days per change.
6. **Military review**: "possible military" list, override toggles, editable callsign/registration/operator lists.
7. **Settings**:
   - sources status, paid-API budget and spend
   - HA connection test, notification channels
   - quiet hours, 2FA and recovery codes
   - attribution (adsb.fi link, adsb.lol ODbL, OpenStreetMap, OurAirports, FlightAware if enabled)

---

## 11. Phased build order

Each phase ends usable, with tests passing and the container working.

### Phase 0 — Skeleton and secure access
- Repo layout; uv/ruff/mypy/pytest; Svelte+Vite scaffold; multi-stage Dockerfile; `compose.yaml` (app, plus an optional `cloudflared` example).
- SQLite migrations; settings.
- **Auth with TOTP**, lockout, CSRF, security headers; first-run setup wizard (password, 2FA, home location).
- CI on GitHub Actions: lint, typecheck, tests, image build; secret scanning (gitleaks).
- Deployment doc for `adsb.<your-domain>` (tunnel or reverse proxy).

### Phase 1 — MVP: "watched aircraft near home" ⭐
- adsb.fi source + adsb.lol failover, query planner (home), rate limiter, circuit breakers.
- tar1090-db + `ranges.json` import; **military classifier v1** (DB flag, address block, derived type stats, callsign list, owner keywords).
- Rules: `type_codes`, `registrations`, `callsign_regex`, `military`, `alt_ft`, `max_distance_nm`, emergency squawks. Condition: **near / visible from home now** (elevation + slant-range check).
- **Home Assistant notifier** (channel, tag, clickAction, test button); dedup + persistent cooldowns.
- Minimal UI: login, rules editor, alert history, aircraft table.
- **Recorder on** and the **KSLN coverage report**.
- ✅ Done when: a matching aircraft (e.g. any military) visible from home produces exactly one phone notification with type, distance, direction, altitude and military reasons, and doesn't repeat during the cooldown.

### Phase 2 — Places, destinations, map, PWA
- Airport targets by ICAO (OurAirports + runways); area targets (polygon editor, paste coords; R-3601 entered as your config); travel viewpoints with dates; **"Use my location"**.
- **Claimed destination:** standing-data routes, adsbdb, plausibility check; **FlightAware AeroAPI plug-in** (opt-in, budget-capped).
- Condition types: `arriving_at` (claimed destination + simple arrival score), `inside` area.
- Map view with SSE; details sheet; PWA install; HA actions (snooze/mute) and channels per rule; multi-rule collapse; quiet hours; rate cap and digest; military review page.

### Phase 3 — Prediction core and tuning
- Ring buffers and kinematics; `nav_heading`/`nav_altitude_mcp` intent; two-tier polling.
- Gate + ensemble; visibility / arrival / area-entry success conditions; confidence; hysteresis; configurable lead times.
- **Runway approach projection**, practice-approach labelling, **local pattern detector**.
- Predicted paths on the map; ETA countdown notifications.
- Synthetic scenarios, replay harness and metrics; **Tuning simulator UI**. Baseline metrics committed.

### Phase 4 — Refinement
- Corroboration factors tuned on replay data; 👍/👎 feedback in the tuning report.
- FAA special-use-airspace search for area targets (FAA ADDS open data); day/night visibility flag.
- Classifier-list updates from the military review loop.

### Phase 5 — Extras
- Web Push channel (backup to HA); optional OpenSky or FR24 position sources; backup/export of places and rules; GHCR image releases for other users.

---

## 12. Public or private repo?

**Recommendation: design it for public from day one. Keep the repo private until the Phase 1 MVP works, then make it public under the MIT license.**

**Why public is a good fit:**

- You'd like to share it, and it's a generic tool: anyone can enter their own home, airports and areas.
- Public repos get unlimited GitHub Actions minutes and free GHCR image hosting, so others can `docker compose up`.
- Others can contribute military callsign/registration lists and classifier fixes, which is the part that improves with more eyes.
- Nothing in the design needs secrecy. All secrets and personal data live in `.env` and `/data`, never in git.

**Why wait until after Phase 1:**

- You get to settle names, structure and the README before anyone looks.
- **Git history becomes public too.** We'll keep it clean from the first commit (gitleaks in CI) so flipping visibility is safe.

**What "public-ready" means for the plan (already reflected above):**

1. **No personal data in the repo.** Home coordinates, your domain, HA URL/token and your places/rules are entered in the app and stored in `/data`. The repo ships generic examples. This plan mentions Salina and KSLN at city level only, never your house. You can drop those lines before going public if you prefer.
2. **No redistributed data:**
   - Test fixtures are synthetic or ODbL-attributed adsb.lol excerpts.
   - tar1090-db, standing-data, OurAirports and adsbdb data are downloaded at runtime.
   - adsb.fi data isn't committed (personal-use terms).
   - FlightAware data is never stored outside `/data`.
3. **Each user runs their own instance.** Load on the free APIs is per-instance and stays within each source's personal-use limits. The README states each source's terms, and paid keys are per user.
4. **License:** MIT (simple, permissive), or AGPL-3.0 if you want modified hosted versions to stay open. MIT is recommended for a hobby tool.
5. **Check with your employer.** Because this would be published under your name, check that your employment agreement doesn't claim side projects. Most don't for personal-time and personal-equipment work, but it's worth a glance.

---

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| A free API changes terms or blocks us (airplanes.live already did) | Pluggable sources + failover; polite polling (≤50% of limits); paid fallbacks identified (§3.2) |
| Poor low-altitude coverage at KSLN | Phase 1 coverage report tells us early. Remedies: receiver (optional) or paid source. |
| Military fighters with transponders off | Can't be fixed with ADS-B data. Expect heavies and helicopters more than fighters; the UI says so. |
| Wrong claimed routes | Plausibility check; filed plans preferred; implausible routes never drive alerts |
| False alarms from maneuvering / pattern traffic | Ensemble probability, hysteresis, pattern detector, tuning simulator |
| Notification spam | Episodes, in-place updates, cooldowns, per-rule daily caps, global cap and digest |
| Internet exposure | TOTP, lockout, CSRF/CSP, minimal unauthenticated surface, tunnel option, audit log |
| Paid API surprise bills | App-enforced monthly budget cap (default = free allowance); lookups only for candidates, cached |

---

## 14. Open questions

1. **Exposure:** Is your domain's DNS on Cloudflare? Do you already run a reverse proxy (NPM, Traefik, Caddy, SWAG) for other containers, or is HA exposed via Nabu Casa? This picks Cloudflare Tunnel vs. reverse proxy.
2. **Home Assistant:** How is HA installed (HA OS VM, or a container on the same Docker host), and what is its LAN URL? Is a long-lived access token OK?
3. **FlightAware AeroAPI:** OK to sign up for the Personal tier (card on file), with the app capped at the free $5/month? Or start free-only?
4. **KSLN pattern traffic** (flight school laps): notify once per aircraft per day (default), always, or never?
5. **Smoky Hill range:** alert on *any* aircraft entering or operating in R-3601, or military only? During active hours, mostly military aircraft are in there anyway.
6. **Visibility defaults:** OK to start with 15° minimum elevation and size-based slant limits of 20 / 12 / 5 nm, then tune with the simulator?
7. **Repo:** OK with "private until MVP, then public under MIT"?
