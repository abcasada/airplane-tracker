# Airplane Tracker — Build Plan (v3)

Status: **draft v3, awaiting approval**. Nothing in here is built yet.
Research and live API checks were done on 2026-10-14/15. Re-check terms before each phase that adds a new source.

Personal ADS-B alert service. It runs in one Docker container on a home server and has a PWA web UI reachable at your own domain. Live aircraft data from free public APIs is enriched and classified (military especially), then run through **filters you build and fine-tune in a dashboard**. A filter can ask:

- is it **visible from where you are**, now or soon?
- is it **headed to an airport**?
- is it **headed into an area**?
- or anything else the aircraft data supports.

Matches are pushed to your Android phone through Home Assistant.

### What changed since v2 (from your answers)

- **Filters are the product.** The filter dashboard (§10) is the centerpiece: a visual builder, a live match preview with a "funnel" view, a backtest against recorded traffic, shadow mode, version history, and one-tap tuning from any alert. "Rules" are now called **filters**.
- **No built-in special cases.** There's no KSLN logic, flight-school or pattern-traffic detection, and no practice-approach labelling. KSLN and Smoky Hill are just places you define, and everything about what alerts is up to your filters. The app supplies generic facts (arrival score, inside-area, visibility, military score…) as filter fields.
- **Repeat suppression is opt-in.** The only always-on de-duplication is "one notification per aircraft per episode, updated in place". "Don't notify about the same aircraft again within X minutes" is an optional filter setting, **off by default** (§9).
- **Free sources only to start.** Paid sources (FlightAware, FR24) stay as future plug-ins (§3.4).
- **Cloudflare Tunnel** deployment (a `cloudflared` container next to the app). Home Assistant is reached directly on the same server.
- **New free data found:**
  - **plane-alert-db**: ~17k "interesting" aircraft with military/government/police categories and tags (ODbL).
  - **adsb.im route API**: working, with a plausibility flag.
- **JetTip** researched: it runs on the **FAA's flight-plan feed (SWIM SCDS)**, not ADS-B. That feed is free and could become an optional source (§3.5).
- **Existing GitHub projects** reviewed for reuse (§11).

---

## 1. Decisions so far

| Topic | Decision |
|---|---|
| Phone / notifications | Android, **Home Assistant Companion app**. HA runs in a container on the same server; long-lived access token approved. |
| Access | `adsb.<your-domain>` via a **Cloudflare Tunnel** container (DNS is on Cloudflare). No ports opened. |
| Server | x86-64. RAM isn't a constraint, but the app stays small. |
| Language | Python |
| Data | **Free sources only** for now; paid sources stay pluggable for later |
| Places | Home viewpoint (Salina); targets such as KSLN and the Smoky Hill range are entered as *your* config. No special handling for any of them. |
| Alerting logic | Entirely user-defined filters. Visibility defaults (15° elevation, 20/12/5 nm slant range by size) are approved as starting values and fully adjustable. |
| Repeat suppression | Optional per-filter setting, **off by default** |
| Repo | Private until the first working version (Phase 1), then public under **MIT** |

---

## 2. Recommended stack

The design goal is **one small container, one process, no extra databases**.

| Layer | Choice | Why |
|---|---|---|
| Backend | **Python 3.13, FastAPI, uvicorn (single worker), asyncio** | One event loop runs the pollers, the evaluation loop and the HTTP API. |
| HTTP client | `httpx` (async, gzip) | |
| Storage | **SQLite** (WAL) via `aiosqlite`, numbered SQL migrations | Config, places, filters (+ versions), alert history, sightings, caches, imported aircraft DBs |
| Live state | In-process memory (per-aircraft ring buffers) | Rebuilt within one poll after a restart |
| Numerics | `numpy` (prediction ensemble), `shapely` (polygon areas) | |
| Validation | `pydantic` v2 | One schema for the filter AST, API, engine and DB |
| Auth | Built-in single-user login + **TOTP 2FA** (argon2id, `pyotp`) | Internet-facing |
| Frontend | **Svelte + Vite** SPA, served by FastAPI as static files | Good fit for a rich, form-heavy filter builder with a small bundle |
| Map | **Leaflet** + OSM raster tiles (URL configurable) | Light |
| Live updates | Server-Sent Events | Heartbeat every 20 s, because Cloudflare closes idle responses after ~100 s |
| PWA | Manifest + small service worker (app shell only) | Android Chrome installs it as an app |
| Tooling | `uv`, `ruff`, `mypy --strict`, `pytest`, `respx`; `npm`, `svelte-check`, `vitest` | |
| Container | Multi-stage build, `python:3.13-slim`, non-root, `/data` volume, healthcheck | Targets: under 200 MB image, under 150 MB RSS (backtests may briefly use more), under 2% CPU average |

### 2.1 Deployment: Cloudflare Tunnel + Home Assistant on the same host

```yaml
# compose.yaml (sketch; real values come from .env)
services:
  airplane-tracker:
    image: airplane-tracker:latest
    volumes: [ "./data:/data" ]
    env_file: .env                      # HA_URL, HA_TOKEN, SECRET_KEY, TRUSTED_PROXIES…
    extra_hosts: [ "host.docker.internal:host-gateway" ]   # reach HA if it runs with network_mode: host
    mem_limit: 256m
    restart: unless-stopped
    # no "ports:" — only cloudflared reaches it
  cloudflared:
    image: cloudflare/cloudflared:<pinned>
    command: tunnel run
    environment: [ "TUNNEL_TOKEN=${CF_TUNNEL_TOKEN}" ]
    restart: unless-stopped
```

- In the Cloudflare Zero Trust dashboard, the tunnel's public hostname `adsb.<your-domain>` points to `http://airplane-tracker:8080`. This is the same pattern as your other apps.
- **Don't** put Cloudflare Access in front: it breaks PWA background requests. The app's own login + TOTP protects it.
  - *Optional extra:* a free Cloudflare WAF rate-limiting rule on `/api/auth/*`.
- Client IP for login lockout comes from `CF-Connecting-IP`, trusted only when the request arrives from the `cloudflared` container's network.
- **Home Assistant:** the app calls HA's REST and WebSocket API at `HA_URL`:
  - `http://host.docker.internal:8123` if HA uses host networking (the usual setup), or
  - the HA container name if both share a Docker network.

  The setup wizard has a "Test connection" button and lists your `notify.mobile_app_*` services to pick from.

**Auth hardening:**

- password + TOTP + 10 recovery codes; 30-day sliding `HttpOnly` / `Secure` / `SameSite=Lax` session
- lockout with exponential backoff per IP and per account
- CSRF (Origin check + token); CSP / HSTS / frame-deny headers; `/docs` off in production
- audit log of logins and changes
- nothing sent off-box (notifications etc.) contains your home coordinates

---

## 3. Data sources

### 3.1 Free live position sources (checked 2026-10-14/15)

| Source | Access | Rate limit | Terms | Live check | Verdict |
|---|---|---|---|---|---|
| **adsb.fi Open Data** — `opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{nm}` (≤250 nm) | No key | **1 req/s**; 4xx responses count toward a **temporary IP block** | Personal, **non-commercial**; **must cite and link adsb.fi** | 200 OK. Has `r`, `t`, `desc`, `ownOp`, `year`, **`dbFlags`**, `nav_heading`, `nav_altitude_mcp`, vertical rates, `squawk`, `emergency`, `category`. | **Primary** |
| **adsb.lol** — `api.adsb.lol/v2/point/{lat}/{lon}/{nm}` (≤250 nm) | No key today (a key "obtained by feeding" is planned) | Unpublished, "dynamic" | **ODbL**; contact them for production use | 200 OK, same JSON shape | **Failover** |
| airplanes.live | — | — | — | **403** "Please contact us…" | Excluded |
| OpenSky Network | OAuth2 | 4,000 credits/day registered | Non-commercial | not polled | Possible later fallback. No type or registration. |

**Coverage around Salina** (live check, 80 nm around KSLN on 2026-10-15):

- 34 aircraft: mostly airliners in cruise, plus bizjets and regional jets below 3,000 ft near other airports.
- **`SAM491` (98-0001, C-32A)** was flagged military via `dbFlags=1`.
- Low-altitude coverage near Salina is measured by a Phase 1 **coverage report** (lowest altitude seen by distance from each place).
- Fighters often fly with ADS-B off. Expect heavies and helicopters more than fighters.

### 3.2 Query planning (efficient polling)

- **Never pulls global feeds.**
  - Each active place gets an **inner circle** (radius + 25 nm, polled every 10 s, 5 s while something is under 2 min out).
  - It also gets an **outer circle** (radius + horizon × 8 nm/min, ≈ 85 nm for a 10-min horizon, polled every 30 s).
  - Everything slows to 60 s when nothing is in reach.
  - Overlapping circles merge. Places within ~15 nm of each other (home, KSLN, the range) collapse into one inner and one outer query.
- Token-bucket budget ≤0.5 req/s per source (half of adsb.fi's limit). The Salina setup uses ~0.13 req/s.
- Circuit breaker per source, failover adsb.fi → adsb.lol. Exponential backoff on any 4xx/429.
- Bandwidth: a few KB per poll around Salina (tens of MB/day). Even very busy JFK was 32–38 KB per 80 nm poll.

### 3.3 Pluggable source interface

`Source` protocol → normalized `Observation` (nm, kt, ft, ft/min, degrees true, UTC epoch seconds). The merge layer keys on ICAO hex and keeps the freshest position.

Kinds:

- **area** (point/radius APIs: adsb.fi, adsb.lol, future OpenSky/FR24)
- **local** (a receiver's `aircraft.json`, if you ever add one)
- **schedule** (filed flight plans, e.g. FAA SWIM, §3.5)

Enrichment plug-ins (routes, flight plans) have their own interface with a per-source budget guard. Paid ones plug in there without touching the engine.

### 3.4 Paid options (deferred, plug-in ready)

| Service | Price | What it would add |
|---|---|---|
| FlightAware AeroAPI Personal | First $5/month of usage free; $0.005 per `/flights/{ident}`; card required | Filed origin/destination for IFR flights, including GA/bizjets |
| Flightradar24 API Explorer | $9/month, 30,000 credits | Live positions with origin/destination |

Not built until you ask. Any paid source must go through an app-enforced monthly budget cap.

### 3.5 What JetTip does, and whether to copy it

[JetTip](https://blog.jettip.net/real-time-faa-data-is-now-powering-jettips-smart-flight-alert-system) alerts on rare aircraft (special liveries, charters, unusual types) coming to *your* airport. Its post of Oct 2023 says it runs on a **real-time feed from the FAA's SWIM Cloud Distribution Service (SCDS)**, *not* ADS-B. It mentions ADS-B only as a possible future addition. That's why it can warn **hours ahead**: broad flight plans (carrier + flight number) appear **24–36 h before departure**, though tail numbers often only near departure.

**FAA SWIM SCDS:**

- **free** for non-operational use (developers, hobbyists); sign-up via the FAA SWIFT portal, per-service access agreement, approval historically within ~72 h
- streams US flight-plan and flight data (e.g. TFMData) over a Solace JMS connection
- sensitive flights, including military, are reportedly **excluded** (per a pre-launch article; to verify)

**Fit for this app:** a good **optional "schedule" source** for *civil* aircraft ("a 747 has filed a flight plan into KSLN for 14:20 tomorrow"), feeding the same filters with an `arrival_filed` fact. It won't help with military traffic. It adds a Solace client dependency and a continuous national stream to filter locally: more CPU/RAM than ADS-B polling, though still modest.

**Recommendation:** Phase 5, only if you want "scheduled visitor" alerts. ADS-B stays the core because it sees military, unfiled and VFR traffic.

---

## 4. Places

| Kind | Defined by | Facts it provides to filters |
|---|---|---|
| **Viewpoint** | Point + elevation; visibility settings; optional date window or GPS expiry ("Use my location") | distance, bearing, elevation angle, slant range, `visible_now`, `predicted_visible` + ETA |
| **Airport** | ICAO code; position, elevation and runways auto-filled from OurAirports | distance, `arrival_score` (generic, §6.2), `claimed_destination == this`, ETA |
| **Area** | Polygon (draw, or paste coordinates; FAA airspace search later) or circle; optional altitude band and active schedule | `inside_now`, time inside, `predicted_entry` + ETA |

Places carry no behavior of their own; filters decide what matters. Place conditions can also target **any active viewpoint** (home, or a travel viewpoint while its dates are active), so one filter follows you on trips: yes/no fields are true if true for any viewpoint, distances take the nearest. Places can carry **active dates** (trips) and **weekly active hours** (e.g. a range's published hours, exposed as the "Within the place's active hours" field). Your starting set is entered in the app and stored in `/data`, not the repo:

- **Home viewpoint**
- **KSLN**: auto-filled
- **Smoky Hill R-3601A/B** area: paste the 7 vertices from 80 FR 60528 (38°45′20″N 97°46′01″W … 38°45′20″N 97°56′01″W), surface–FL230

Default visibility settings for a viewpoint (approved as a starting point, all editable):

- minimum elevation angle 15°
- maximum slant range 20 nm for heavy/large, 12 nm for medium, 5 nm for light/rotorcraft

---

## 5. Enrichment and military classification

### 5.1 Enrichment sources (downloaded at runtime, cached in SQLite; never committed)

| Need | Source | License | Notes |
|---|---|---|---|
| Type, registration, description, year, owner, military/LADD/PIA flags | **tar1090-db** `aircraft.csv.gz` (~622k rows) + `ranges.json` (32 military address blocks) | No license file; personal use | Weekly refresh. Also yields `type_stats` (share of military airframes per type). |
| **"Interesting" aircraft list** with categories and tags | **plane-alert-db** (`sdr-enthusiasts/plane-alert-db`) — `plane-alert-db.csv`, ~17.3k aircraft | **ODbL 1.0 / DbCL 1.0** | Columns: ICAO, registration, operator, type, ICAO type, **CMPG** (Civ/Mil/Pol/Gov), **Tag 1–3**, **Category** (54, e.g. *USAF*, *Zoomies* (fast jets), *Oxcart* (intel), *Gunship*, *Special Forces*, *UAV*, *Toy Soldiers* (armies), *Dictator Alert*…), link. Weekly refresh. Optional `plane_images.csv` (~12k photo links) for the UI. |
| Operator from callsign | VRS standing-data airlines | CC0 | `ownOp` is the registered *owner* (often a bank trustee) |
| Routes by callsign | **adsb.im** `POST /api/0/routeset` (batch, returns multi-leg routes + **`plausible`** flag; tested OK). Further sources (standing-data, adsbdb) can plug into the same `RouteService` if adsb.im ever stops answering. | adsbdb route data not redistributable | Cached 24 h (misses 6 h). Batched, ≤1 request per 30 s. adsb.lol's routeset returned empty. Military and GA callsigns come back "unknown", as expected. Our own plausibility check allows 30 nm off-route near airports, growing to 150 nm mid-route. |
| Airports, runways, elevations | OurAirports | Public domain | Airport places, arrival score |
| Viewpoint elevation | USGS Elevation Point Query Service (once per viewpoint) | Public | Manual override |

### 5.2 Military classifier (FR24-style, explainable)

FR24's military filter isn't publicly documented; it appears to use their own aircraft database. Ours **combines independent signals into a score with visible reasons**. The score and each signal are filter fields, so you decide how strict to be.

| Signal | Default weight | Source / example |
|---|---|---|
| `dbFlags` military bit (API) or tar1090-db flag | 0.9 | `SAM491` 98-0001 C-32A had `dbFlags=1` |
| **plane-alert-db CMPG = Mil** | 0.9 | ~9.9k military airframes, with category/tags explaining *what* it is |
| ICAO address in a military block | 0.85 | tar1090-db `ranges.json` (US `adf7c8–afffff`) |
| Military-only type (≥95% of airframes military) | 0.8 | Derived from tar1090-db: 42 types, e.g. `C17`, `K35R`, `B52`, `B1`, `F35`, `F15`, `A10`, `C30J`, `V22`, `P8`, `E6`, `R135`, `C5M` |
| Dual-use type | 0.2–0.5 (scaled by share) | `H60`, `C130`, `F16`, `T38`, `TEX2`, `BE40` (T-1A), `B350` (C-12), **`B762` (the KC-46A is typed as a 767-200)** |
| Owner/operator text | 0.8 | "AIR FORCE", "NAVY", "ARMY", "MARINE", "NATIONAL GUARD", "COAST GUARD" |
| Callsign list | 0.6 | `RCH`/`REACH`, `SAM`, `SPAR`, `EXEC`, `VENUS`, `PAT`, `CNV`, `NAVY`, … (editable) |
| Tactical-callsign heuristic | 0.3 | Word + 2–3 digits with no airline match, e.g. `HOOK21` |
| Registration pattern | 0.6 | US serial `NN-NNNN(N)`, UK `ZZ123`, … (editable regexes) |
| **Your overrides** | Final | Mark any hex or registration military or not |

- Score = 1 − ∏(1 − wᵢ). Weights and lists are editable in the dashboard.
- **Government** and **police** are separate classes (plane-alert-db CMPG Gov/Pol, owner text such as FAA, CBP, "STATE OF …"). FR24 lumps these together with military; here they're separate fields.
- Validation: compare against the `dbFlags`-only baseline on recordings. The dashboard lists "possible military" (score 0.3–0.6) for review.

---

## 6. Destination and arrival facts (generic)

### 6.1 Claimed destination

- Resolution order: adsb.im routeset → VRS standing-data → adsbdb. Later, optional paid or SWIM plug-ins go first.
- **Plausibility check** always runs: adsb.im's own `plausible` flag, plus our own great-circle check. adsbdb once listed DAL1061 as BOS→SFO while it sat at 75 ft beside JFK.
- Filter fields:
  - `origin`, `destination`, `route_legs`
  - `route_source`
  - `route_plausible`
- Shown as "→ KSLN (adsb.im, plausible)" or "→ KSLN? (route DB, implausible)".

### 6.2 Arrival score (any airport place, no airport-specific logic)

A 0–1 `arrival_score` per aircraft per airport place, from generic signals:

- distance and altitude above the field
- descending, or level low; `nav_altitude_mcp` below current altitude
- decelerating
- track toward the airport, or aligned with any of its runways' extended centerlines
- altitude consistent with a ~3° descent

High scores also switch the predictor to a runway-approach projection (§7). How you use the score is up to your filters. For example:

- `arrival_score ≥ 0.7`
- `destination = KSLN OR arrival_score ≥ 0.5`
- exclude whatever operators you don't care about

### 6.3 Area facts

`inside_now`, `seconds_inside`, `predicted_entry` (+ ETA and confidence), and `in_altitude_band`. The area's optional schedule (e.g. active Mon–Sat 0900–1700) is just another field you can filter on.

---

## 7. Prediction approach

The goal is to produce **predicted facts** (`predicted_visible`, `predicted_entry`, `predicted_arrival`) with ETA and confidence, early enough to step outside, without false alarms from maneuvering aircraft. Filters consume them like any other field.

### 7.1 Geometry
- Local tangent plane per place; error negligible within 250 nm.
- CPA `t* = clamp(-p·v/|v|², 0, H)`; circle-entry ETA from the quadratic; curved paths sampled at 10 s; polygon entry via `shapely`.
- **Visibility:**
  - elevation angle `atan2(alt_ft − viewpoint_elev_ft, ground_dist_ft)` ≥ the viewpoint's minimum, **and**
  - slant range ≤ the size-based limit.
- Optional day/night flag from sun position.

### 7.2 Kinematics per aircraft
From a ring buffer of the last ~40 positions:

- smoothed track/speed; turn rate over ~60 s and its variability over ~2 min (the "maneuvering" signal)
- vertical: `geom_rate`, falling back to `baro_rate`
- **autopilot intent:**
  - a `nav_heading` more than 5° off the current track ⇒ project a standard-rate turn onto it
  - `nav_altitude_mcp` caps the climb/descent

### 7.3 Two-stage prediction
1. **Gate** (all aircraft, every update): straight line + `nav_heading` turn over horizon `H` (default 10 min, configurable). Candidates pass near or toward a place.
2. **Ensemble** (candidates only, numpy): 64 sampled trajectories.
   - track/speed noise by data quality
   - turn rate = observed (decaying) + maneuver-scaled noise
   - some samples follow the `nav_heading` turn
   - vertical rate levels at the selected altitude

   Built once per aircraft per cycle in an aircraft-centred plane and shared by every place (each place only shifts it), vectorized over time with closed forms. About 40 ms per cycle for 60 aircraft and 3 places.

   **Arrivals** don't use the ensemble: `predicted_arrival` comes from `arrival_score` (§6.2) with ETA = (distance + 3 nm if not lined up) / ground speed. Its confidence is discounted while the aircraft is turning or maneuvering (≈0.8 at 1°/s, ≈0.14 at 3°/s) unless it's lined up with a runway, because the score reads the current heading. That's a generic kinematic factor, not pattern detection: the `arrival_score` field itself is unchanged.

   Outputs:
   - `p_success` per predicted fact
   - `eta_p10/p50/p90`
   - CPA, elevation angle, direction
   - predicted-path polyline + uncertainty band

### 7.4 Confidence and lead time
```text
confidence = p_success × q_data × q_horizon × q_corroborate   (clipped to [0, 1])
  q_data        position age, MLAT/TIS-B, NIC/NAC, history length (<30 s ⇒ cap 0.5)
  q_horizon     exp(−ETA/τ); τ ≈ 12 min cruise, ≈ 5 min below 5,000 ft AGL
  q_corroborate claimed destination agrees/disagrees; arrival score; stability over recent updates
```

- **Hysteresis:** a predicted fact turns true at ≥ θ on 2 consecutive evaluations, and false after 3 evaluations below θ − 0.15.
- Lead time and minimum confidence cascade global (Settings → Prediction, 10 min / 0.6 to start) → place (optional override) → filter. At the filter level they're ordinary conditions on the `*_eta_s` and `*_confidence` fields (e.g. "visible ETA ≥ 90 s" for a minimum lead), so they work the same in the live preview and the backtest.
- `predicted_*` is true only while the hysteresis is on. An aircraft that's already visible/inside counts as predicted with ETA 0, so "predicted visible" filters don't drop it as it arrives.

### 7.5 Testing against recorded flight data
1. **Synthetic scenario tests:**
   - straight overflight; near miss at 1.05·R; turns toward/away
   - orbit; holding; straight-in and turn-to-final approaches
   - polygon entry; racetrack inside a polygon
   - stale/MLAT jitter; squawk glitch
2. **Recorder** (on from Phase 1): compact hourly NDJSON.gz under `/data/recordings`, 14-day retention (configurable), scenarios can be pinned.
3. **Replay harness** with an injected clock. Ground truth is taken from the recording's future. It reports:
   - precision, recall, false alarms per day
   - achieved lead time, ETA error, calibration (reliability, Brier)
   - classifier agreement vs `dbFlags` (Phase 4)

   Parameter sweeps; tune on some days, validate on others. The same engine powers the dashboard **backtest** (§10.3).

   Implemented as `app/replay` and `python -m app.replay [--from 2026-10-01T08:00] [--days N] [--lead S] [--confidence C] [--json]`, using the places, filters and settings in `DATA_DIR`. Definitions:
   - **event**: the aircraft becomes visible from a viewpoint (`visible_now`), enters an area (`inside_area`), or reaches an airport (within 2 nm and below 1,000 ft above the field, or on the ground within 3 nm). Onsets within 2 min of the last one are the same event (flicker). Events less than 60 s after first contact count as events but not as *predictable* ones (the recall denominator).
   - **alarm**: `predicted_*` turns on while the event hasn't happened yet. It's true if the event follows within lead time + 2 min, otherwise a false alarm. Alarms still open when the recording ends are left out.
   - **calibration**: confidence sampled every 30 s per pair before the event; outcome = event within the lead time. Reports Brier score and ECE over 10 bins.

   First numbers (defaults): on 5.5 h of real night-time traffic around the home example, *visible*: predictor recall 0.93 (14/15), precision 0.88, median lead 153 s (p10 126 s), median ETA error 6 s, ECE 0.09; adsb-alarm baseline precision 0.54 with about 6× the false alarms. Median lead is under the 3 min target; tuning on more days is Phase 4.
4. **Baseline to beat:** the simple heuristic used by [adsb-alarm](https://github.com/stfnhh/adsb-alarm): "track within 90° of the bearing to you and distance decreasing".
5. **CI fixtures:** synthetic, or short attributed adsb.lol (ODbL) excerpts only.
6. **Initial targets:**
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

places(id PK, name, role ('viewpoint','airport','area'), lat, lon, elevation_ft, radius_nm,
       polygon_geojson, alt_floor_ft, alt_ceiling_ft, airport_icao, visibility_json,
       active_from, active_until, schedule_json, defaults_json,   -- lead/confidence overrides
       enabled, source ('manual','geolocation','airport_lookup'), created_at)
runways(airport_ident, le_ident, he_ident, le_heading, he_heading, le_lat, le_lon, he_lat, he_lon, length_ft)

filters(id PK, name, enabled, mode ('live','shadow'), folder, sort_order,
        current_version_id, created_at, updated_at)
filter_versions(id PK, filter_id, version, definition_json,   -- the filter AST (below)
                note, created_at)
exclusions(id PK, scope ('global', filter_id), kind ('hex','reg','type','operator','callsign','expr'),
           value, until, note, created_at)                     -- quick mutes from notifications/alerts

military_overrides(key PK, is_military, note, created_at)
classifier_settings(key PK, value_json)  -- signal weights, callsign/registration/operator lists

alerts(id PK, dedup_key, hex, callsign, reg, type_code, filter_id, filter_version_id, place_id,
       mode ('live','shadow'), state ('predicted','active','ended'),
       first_seen_at, predicted_at, entered_at, closest_at, ended_at,
       best_confidence, eta_at_alert_s, cpa_nm, cpa_alt_ft, cpa_bearing_deg, elevation_deg,
       facts_json,          -- every field value at alert time (powers "why did this fire?")
       feedback ('up','down', NULL))
notifications(id PK, alert_id, channel, sent_at, kind ('new','update','emergency','digest'),
              payload_json, ok, error)
suppression_state(key PK, last_notified_at, count_window)     -- only used by opt-in suppression settings

sightings(hex, place_id, day, type_code, first_at, last_at, min_dist_nm, PK(hex, place_id, day))   -- history fields

aircraft_db(icao PK, reg, type_code, flags, descr, year, own_op, db_version)
plane_alert(icao PK, reg, operator, type, icao_type, cmpg, tag1, tag2, tag3, category, link, db_version)
mil_ranges(start_hex, end_hex)
type_stats(type_code PK, airframes, military_share)
icao_types(type_code PK, description, wtc, engine_type, engine_count)  -- if available from tar1090-db
airlines(icao PK, name, iata, callsign_tele, country)
airports(ident PK, icao, iata, name, lat, lon, elevation_ft, type)
routes(callsign PK, legs_json, plausible, source, fetched_at)
cache(namespace, key, value_json, fetched_at, ttl_s, negative, PK(namespace,key))
source_status(name PK, last_ok_at, last_error, consecutive_failures, requests_today)
```

---

## 9. Notifications (Android + Home Assistant)

Notifications go to `notify.mobile_app_<phone>` via HA's REST API. Delivery works off-network through HA's push relay. The cap is 500/day/device.

| HA Android feature | Use |
|---|---|
| `channel` | Each filter picks an Android **notification channel** (e.g. "Aircraft – Military"); sound, vibration and importance are then set per channel in Android |
| `importance: high`, `priority: high`, `ttl: 0` | Immediate delivery |
| `tag` | **Update in place** (ETA changes, predicted → overhead) instead of stacking |
| `chronometer` + `when` | Live ETA countdown. When the ETA moves by ≥60 s, the notification is re-sent in place with `alert_once` (silent): at most 3 updates per notification, ≥60 s apart, counted against the daily ceiling. Per-filter switch, on by default. |
| `timeout` | Auto-dismiss after it has passed |
| `clickAction` | Opens the PWA on that aircraft |
| `actions` | "Mute this aircraft…", "Mute this type for this filter…", "Snooze filter 1 h", "👎 not interesting". Received via HA's WebSocket API (`mobile_app_notification_action`) and written as exclusions/feedback. |
| `image` | Aircraft photo from plane-alert-db links (public URLs, no home data). Planespotters.net photos are shown only in the web UI, because their terms require a visible photographer credit and link next to the image, which a notification can't carry. |
| `channel: alarm_stream` | Optional per filter: sounds even in Do Not Disturb |

**Message content is a per-filter template** built from any fields. The default looks like:

```text
✈ C-17A Globemaster III · visible from Home in ~4 min   [countdown]
RCH452 · USAF (military: DB flag, plane-alert Mil/USAF, type C17)
Passes 1.2 nm NW at ~3,500 ft AGL, ~45° up, heading SE · 280 kt, descending
Route: KDOV → KSLN? (implausible) · Arrival score KSLN 0.82 · confidence 81%
Filter: Military over home
```

**De-duplication:**

- **Always on (not a filter):** one notification per *aircraft × filter × place episode*, updated in place. Without this, every 10 s poll would re-notify.
  - An episode ends when the condition has been false for 2 min, or the aircraft has been lost for 5 min.
  - If it matches again later, that's a **new episode and a new notification**.
  - Multiple filters matching one aircraft at once → one combined notification.
- **Optional (all off by default), per filter or global:**
  - "don't notify about the same aircraft again within X minutes"
  - max notifications per day
  - global rate cap with digest
  - quiet hours (with per-filter bypass)
- **Safety ceiling:** a configurable ceiling (default 400/day) keeps you under HA's 500/day limit. If it's ever reached, the dashboard shows a warning.

**Backup channel:** Web Push to the installed PWA (reliable on Android) can be added in Phase 5 behind the same `Notifier` interface.

---

## 10. The filter dashboard (centerpiece)

The design goal is that tuning "what reaches my phone" is fast, visual, safe to experiment with, and explainable.

### 10.1 Filter model

A filter is a **JSON expression tree**: nested groups **ALL / ANY / NONE** of conditions `field op value`. Each filter also has:

- **places**: each place-related condition names its place ("Distance from Home < 10 nm", "Inside area Smoky Hill"), so one filter can combine several places. A *display place* sets the distance and direction used in notification text.
- **timing**: predicted facts (Phase 3) are fields like any other (`predicted_visible`, ETA, confidence), so "now" vs "soon" is expressed with conditions
- **notification settings**: channel, priority, template, optional suppression

Global and per-filter **exclusions** (quick mutes) apply on top.

**One engine** compiles the tree into fast Python predicates. It is used identically for:

- live alerting
- the live preview
- the backtest over recordings

Preview and backtest therefore always agree with what would actually notify.

**Field catalog** (each with type, units, help text and autocomplete):

| Group | Fields |
|---|---|
| Identity | hex, registration, callsign, flight number, airline (from callsign), owner/operator |
| Aircraft | ICAO type, type description, manufacturer, emitter category, wake class, engine type/count (if available), year built |
| Classification | military score + each military signal, government, police, plane-alert CMPG / category / tags, LADD, PIA |
| State | altitude (baro/geo, MSL/AGL), ground speed, vertical rate, track, squawk, emergency, on-ground, position source (ADS-B/MLAT/TIS-B), data age |
| Relative to place | distance, bearing (or compass sector), elevation angle, slant range, `visible_now`, `inside_now`, `seconds_inside`, `arrival_score` |
| Predicted | `predicted_visible` / `predicted_entry` / `predicted_arrival`, ETA, CPA distance, CPA altitude, confidence |
| Route | origin, destination, legs, route source, route plausible |
| History | times seen at this place in the last N days, first-ever sighting, last seen. This enables JetTip-style **"rare visitor"** filters without any built-in definition of "rare". |
| Time | local time of day, day of week, date range, day/night |

*Built (Phase 4):* a sighting is a local day on which the aircraft came within a place's **watch radius** (`radius_nm`). Fields, all per place: `visits_prev_30d`, `days_since_visit`, `first_visit` and `type_visits_prev_30d` (days any aircraft of the type was there). They count only days **before today**, so being here now doesn't change them, and replays and backtests see only the history before the replayed day. History is kept 180 days. Today's sightings stay in memory and are flushed every minute; past days load lazily per aircraft and type into bounded caches. Day/night: `sun_elevation_deg`, `daylight` (sun above −0.833°) and `dark` (below −6°, after civil twilight), per place, from the NOAA solar equations.

**Operators:** `=`, `≠`, `in list`, `glob`, `regex` (validated), `<`, `≤`, `between`, `exists`, `is true/false`.

### 10.2 Builder and live preview
- **Visual builder:** add/remove/reorder conditions, nest groups, toggle a condition off without deleting it.
  - Value pickers with autocomplete: type codes with descriptions, operators seen recently, plane-alert categories, places.
  - A **plain-English summary** updates as you edit, e.g. "Military (score ≥ 0.6) AND predicted visible from Home within 10 min, 06:00–23:00".
- **Live preview:** aircraft currently in reach that match, updated via SSE, and highlighted on a mini-map.
- **Funnel view:** for each condition, how many aircraft remain, e.g. "34 in reach → 3 military → 2 below 15,000 ft → 1 predicted visible". It shows exactly which condition is too strict or too loose.
- **"Why not?"**: pick any aircraft and see which condition rejected it.
- **Advanced:** a JSON editor with schema validation, for copy/paste and power edits.

### 10.3 Backtest (what-if)
- Run the draft filter over the last 1/3/7/14 days of recordings and see:
  - notifications per day (bar chart)
  - the list of would-be alerts (time, aircraft, mini-map, facts)
  - the **diff against the saved version**: alerts added (+) and removed (−)
- Tune sliders (lead time, confidence, elevation angle, slant range, suppression) and re-run.
- Runs in a background task with progress reporting and is capped by date range, so it never stalls live alerting.

Built (Phase 3): "What if?" on the filter editor runs the draft (and the saved version, when the draft differs) over the last 1/3/7/14 days: daily alerts and notifications, totals, and the aircraft added (+) or removed (−). One backtest runs at a time as an asyncio task that yields after every batch; the predictor runs only if the filter uses prediction fields. Episodes, exclusions and the filter's opt-in suppression use the live code (`suppression_reason` is shared). It uses today's places, mutes, settings and cached routes, and global cross-filter repeat suppression is not modelled because other filters aren't replayed. Not built yet: per-alert mini-map/facts and in-panel sliders (edit the draft and re-run instead).

### 10.4 Safe changes
- **Shadow mode:** a filter evaluates live and logs alerts to history, marked "shadow", but doesn't push. Promote it to live when happy.
- **Versions:** every save creates a version, with diff and one-click revert. Alerts record which version fired them.
- **Import/export** filters as JSON (also how people will share filters once the repo is public).

### 10.5 Tuning from real alerts
- On any alert (in the app or via notification buttons):
  - "Mute this aircraft (1 h / 1 day / forever)"
  - "Exclude this type / operator / callsign pattern from this filter"
  - "Make a new filter from this aircraft"
  - "👍 / 👎"
- **Alert history** with "why did this fire?" (the condition tree evaluated against stored facts) and per-filter stats: notifications per day, 👍/👎 ratio.

### 10.6 Other screens
- **Map:** places (circles, airports with runways, polygons); aircraft colored by matched filter; predicted paths with uncertainty bands; details sheet (all fields, military reasons, route, photo).
- **Places:** home, airports by ICAO, area polygons (draw or paste), travel viewpoints with dates, "Use my location", visibility settings.
- **Military review:** "possible military" list, overrides, signal weights and lists.
- **Settings:** sources status, HA connection test and channel list, defaults (global lead/confidence/visibility), safety ceiling, 2FA, data refresh, attribution (adsb.fi link, adsb.lol ODbL, plane-alert-db ODbL, OpenStreetMap, OurAirports).
- Works on phone (stacked cards, bottom sheets) and desktop (side-by-side builder and preview).

---

## 11. Existing projects and what we can reuse

| Project | What it is | Reuse |
|---|---|---|
| [plane-alert-db](https://github.com/sdr-enthusiasts/plane-alert-db) | ~17k curated "interesting" aircraft, CMPG + 54 categories + tags (ODbL/DbCL) | **Use as data** (runtime download): military signal and filter fields |
| [tar1090-db](https://github.com/wiedehopf/tar1090-db) | Aircraft DB + military address ranges | **Use as data** (already planned) |
| adsb.im routeset API | Callsign → route with plausibility | **Use** as the primary route lookup |
| [adsb-monitor](https://github.com/jchisholm59/adsb-monitor) (MIT, Node) | Closest in spirit: adsb.lol/adsb.fi area polling or local receiver, HA webhook/ntfy alerts, military via tar1090-db + address blocks + callsigns, approach detection (within 20 nm, below 6,000 ft, descending) | **Reference** for heuristics and lists (MIT: borrow with attribution). Not forked: it's built around a HA Lovelace card, with no prediction or filter engine. |
| [aerodrome](https://github.com/preston-peterson/aerodrome) (MIT, Python/FastAPI/SQLite) | Same stack. Watchlists, military highlighting (address ranges + callsign prefixes + lists), ntfy | **Reference** for callsign-prefix lists and dashboard ideas. Local receiver only; PRs disabled. |
| [adsb-aircraft-tracker](https://github.com/hook-365/adsb-aircraft-tracker) (MIT, HA custom component) | Military/low-altitude/emergency alerts inside HA | **Reference only.** Needs a local feeder; no prediction. |
| [adsb-alarm](https://github.com/stfnhh/adsb-alarm) (MIT, Go) | Polls adsb.fi; webhook when an aircraft of chosen categories is inbound | **Replay baseline** for our predictor |
| [plane-notify](https://github.com/Jxck-S/plane-notify) (GPL-3.0, Python) | Takeoff/landing notifications for specific aircraft | Ideas only. GPL: no code copied. |
| [docker-planefence](https://github.com/sdr-enthusiasts/docker-planefence) / Plane-Alert | Overhead log + watchlist for local receivers | Ideas only; local receiver required |
| [ha_adsb_lol](https://github.com/vingerha/ha_adsb_lol) | HA integration for adsb.lol | Archived Jan 2026; not usable |

**Conclusion:** nothing existing combines public-API polling, prediction with confidence, and a full filter dashboard. We build our own core and reuse the **data sets** (plane-alert-db, tar1090-db, OurAirports, standing-data) and the adsb.im route API. We borrow lists and heuristics only from MIT projects, with attribution in `NOTICE`.

---

## 12. Phased build order

Each phase ends usable, with tests passing and the container working. **The filter engine and builder come early**, since tuning filters is the core activity.

### Phase 0 — Skeleton and secure access
- Repo layout; uv/ruff/mypy/pytest; Svelte+Vite+vitest scaffold; Dockerfile; `compose.yaml` with app + `cloudflared` + `.env.example`.
- SQLite migrations; auth with TOTP, lockout, CSRF, headers; setup wizard (password, 2FA, home viewpoint, HA URL/token with connection test and notify-service picker).
- CI: lint, typecheck, tests, image build, gitleaks.
- Deployment doc: Cloudflare Tunnel public hostname, HA token.

### Phase 1 — MVP: filters → phone ⭐ (repo goes public after this)
- adsb.fi + adsb.lol failover, query planner, rate limiter, circuit breakers.
- tar1090-db, `ranges.json` and **plane-alert-db** import; **military classifier v1** with reasons.
- **Filter engine** (expression tree, fields: identity, aircraft, classification, state, distance/bearing/elevation/visible-now for viewpoints); exclusions; emergency-squawk starter filter (editable, like everything else).
- **Builder v1:** visual groups and conditions, autocomplete, plain-English summary, **live preview + funnel + "why not?"**.
- HA notifier: channel, tag, clickAction, template, test button. Episode de-duplication; optional suppression settings (off).
- Alert history with "why did this fire?"; recorder on; coverage report.
- ✅ Done when you can build e.g. "military visible from home" in the dashboard, see it in the live preview, and get exactly one updating notification per aircraft episode.
- Then: README, MIT license, `NOTICE` attributions, history check with gitleaks → **flip the repo public**.

### Phase 2 — Places, routes, map, safe tuning
- Airport places (OurAirports + runways), area polygons (draw/paste), travel viewpoints, "Use my location".
- Route lookups (adsb.im → standing-data → adsbdb) with plausibility; `arrival_score`; area facts. All as filter fields.
- Map view with SSE and details sheet; PWA install.
- **Shadow mode, versions with diff and revert, import/export.**
- Notification action buttons (mute/exclude/snooze/👎) via HA WebSocket; per-filter channels and alarm stream; optional photos.
- Military review page.

### Phase 3 — Prediction + backtest
- Ring buffers, kinematics, autopilot intent; two-tier polling.
- Gate + ensemble; `predicted_*` facts, ETA, confidence, hysteresis; runway-approach projection for high arrival scores.
- Predicted paths on the map; countdown notifications.
- Synthetic scenarios, replay harness and metrics vs the adsb-alarm baseline.
- **Dashboard backtest (what-if)** with daily counts and diff vs the saved version.
- *Status: done* (see §7.3–7.5, §9 and §10.3 for what was built and what's deferred).

### Phase 4 — History and refinement
- Sightings history fields ("rare visitor" filters); 👍/👎 stats per filter. *Done.*
- FAA airspace search for area polygons; day/night field. *Done:* the area form searches FAA ADDS special use airspace by name or near a point (on demand, cached 7 days), merges stacked sectors with the same outline (R-3601A + B → surface to FL230), converts AGL limits with the ground elevation, and pre-fills active hours when the published times are a single window (time zone from the FAA GMT offset and DST flag).
- Tune confidence factors on replay data; classifier-list updates from the review loop.

### Phase 5 — Optional sources and extras
- **FAA SWIM SCDS "filed flight plan" source** (JetTip-style "scheduled visitor" facts for civil flights), if wanted.
- Paid plug-ins (FlightAware, FR24) if wanted, behind budget caps; OpenSky.
- Web Push backup channel; GHCR image releases; backup/restore of places and filters. *Backup/restore done:* Settings → Backup downloads one JSON file (places, filters, mutes, settings, military overrides; never credentials). Restore merges, remaps place ids inside filters and mutes, skips places and filters that already exist, and keeps this install's home.

---

## 13. Public repo plan (decided)

**Private until Phase 1 works, then public under MIT.**

- **Never committed:** home coordinates, domain, HA URL/token, real places/filters. These live in `.env` and `/data`. The repo ships generic examples. This plan names Salina/KSLN at city level only; drop those lines before going public if you prefer.
- **No redistributed data:** fixtures are synthetic or attributed adsb.lol (ODbL) excerpts. All other datasets are downloaded at runtime; adsb.fi, adsbdb and paid-source data are never committed.
- Each user runs their own instance with their own API usage. The README states each source's terms.
- gitleaks in CI from Phase 0, so history is clean when it flips.
- `NOTICE` file for MIT-licensed lists and heuristics borrowed from other projects, and dataset attributions.
- Since it'll be public under your name, do a quick check that your employment agreement doesn't claim side projects.

---

## 14. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Free API changes terms or blocks us | Pluggable sources + failover; ≤50% of limits; paid/SWIM plug-ins identified |
| Low-altitude coverage gaps | Coverage report in Phase 1; options: receiver or paid source |
| Military aircraft with transponders off | Unfixable with ADS-B; the UI says so |
| Wrong claimed routes | adsb.im plausibility flag + own check; route plausibility is a filter field |
| False alarms from maneuvering aircraft | Ensemble probability, hysteresis, confidence as a filter field, backtest |
| Notification overload while tuning | Shadow mode, backtest before going live, quick mutes from notifications, safety ceiling |
| Backtests competing with live alerting | Background task, date-range cap, live loop has priority |
| Internet exposure | Cloudflare Tunnel (no open ports), TOTP, lockout, CSRF/CSP, audit log |

---

## 15. Open questions

1. **Approve this plan (v3) and start Phase 0?**
2. **Aircraft photos in notifications:** OK to show plane-alert-db photo links? They load from third-party sites when the notification arrives.
3. **FAA SWIM (JetTip-style scheduled arrivals):** keep it in Phase 5 as an option, or drop it? It only helps with civil traffic.
