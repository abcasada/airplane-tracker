# Handoff (2026-11-17)

For whoever (human or LLM) continues this project. Read `CLAUDE.md` first (rules and architecture
map), then this file. Deeper docs: `docs/PLAN.md` (design and history), `docs/SOURCES.md` (data
sources and sign-ups), `docs/NTFY.md`, `docs/DEPLOY.md`. Delete or update this file when it stops
being true.

## Where we are

Personal ADS-B alert service: polls every enabled source (adsb.fi, adsb.lol, optional FlyItalyADSB,
OpenSky and the user's own receiver) around the user's places, merges them by hex,
enriches/classifies aircraft (military focus), runs user-built filters, and pushes matches to the
phone through the user's own **ntfy** server. One Docker container; PWA web UI with a live map.

- **Built and on `main`:** Phases 0–3 and most of 4 (auth+TOTP, ingest, enrichment, military
  classifier, filter engine and dashboard, places/airports/areas, prediction with ETA, replay
  harness, "What if?" backtest, sightings history, day/night, FAA airspace search, backup/restore),
  ntfy notifications, follow-anywhere (`watch.py`), take-off/landing notifications.
- **2026-11-10 (PR #5):** military review suggestions (`classify/suggest.py`, settings for minimum
  marks and "bring back when evidence doubles"); multi-source merge with per-source budgets and a
  Sources report with per-source on/off switch; FlyItalyADSB, OpenSky, local receiver sources;
  simple filter mode (`frontend/src/lib/simple.ts`).
- **2026-11-17 (branch `claude/integration-2`):**
  - Simple mode "At or headed to your places" rows: Arriving soon (`predicted_arrival`), Inside or
    entering soon (`predicted_entry`), Distance and Height above place; place pickers default to
    the first airport/area place. "At the airport" = small distance + height 0 ft.
  - **Flightradar24** (`sources/fr24.py`, `ingest/fr24.py`, `ingest/credits.py`): never polled;
    "Look up on Flightradar24" on the aircraft page and gap-fill for followed/recently matched
    aircraft the free feeds lost ≥90 s. Monthly credit cap (`FR24_MONTHLY_CREDITS`, default
    20,000), even-pace limit for gap-fill, ≤1 request/12 s. Off until `FR24_API_KEY` is set.
  - **FAA registry + hexdb.io** (`enrich/faa.py`, `enrich/hexdb.py`, migration 0008): weekly
    download of `ReleasableAircraft.zip` (streamed import, ~5 MB extra RAM), fills US civil
    aircraft fields after tar1090-db; fields `registrant_type`, `faa_aircraft_type`,
    `faa_engine_type`; classifier setting `government_registrant_types`. hexdb.io only for
    tracked aircraft still missing data (≥2 s apart, ≤2,000/day, cached).
  - **FAA SWIM** (`sources/swim.py`, migration 0009): TFMS flight data over Solace (optional
    `swim` extra, installed in the image, ~7 MB RAM only when configured). Keeps flights to/from
    airport places and followed registrations; "Filed plan" filter fields (`has_filed_plan`,
    `filed_to_place`, `filed_eta_s`, …); filed plans take priority as routes. Off until the five
    `SWIM_*` env vars are set.
  - `docs/NTFY.md` now covers ntfy behind the shared tunnel and an existing ntfy server.
- **Last verified 2026-11-17 (`claude/integration-2`):** 231 backend tests, ruff and
  `mypy --strict` clean; frontend check, 37 tests and build clean (host has no Node: run npm in
  `node:22-slim`). **None of the new UI has been looked at in a browser.**
- **Deployment:** shared Cloudflare tunnel; the app publishes `127.0.0.1:8790` and the tunnel
  routes `adsb.<domain>` to it (`docs/DEPLOY.md`). PR #5 is deployed and healthy (2026-11-17).
  **First-run setup still not done** (0 users, 0 places): nothing is polled, recorded or alerted.
  ntfy v2.34.0 runs on the same server with `deny-all` auth; the app container reaches it via its
  public URL. Setup steps for the user: `docs/NTFY.md` step 2 (a write-only `airplane-tracker`
  ntfy user + token).

## Not yet verified against real services

- **ntfy** (mock-tested only). First real check: Settings → "Save & send test notification".
- **Take-off/landing detection** on live traffic (synthetic flights only). Watch for false
  "landed (probably)"; thresholds are constants at the top of `events.py`.
- **FAA registry download** answered HTTP 503 to a plain request with our User-Agent. If it keeps
  failing, download `ReleasableAircraft.zip` in a browser into `/data/datasets/`. The FAA column
  layout doc returned 403, so code tables come from secondary sources: check the first import.
- **FR24**: per-request credit costs aren't published; `FR24_CREDITS_PER_FLIGHT` (default 8) is an
  estimate. Calibrate with "Check FR24's own count" in the Sources report. Terms/attribution and
  whether the plan resets monthly or on the subscription anniversary are unverified.
- **SWIM** message details (etaType/status values, airport code forms) need checking on first
  connection. The FAA disables SCDS subscriptions idle for ~60 days.

## User decisions

- ntfy only (self-hosted, own hostname); no Home Assistant, no Web Push, **no container registry
  publishing**.
- **Purpose:** what is at or headed to the airport or bombing range, not what's visible from home.
  Design filter UI and defaults around airport/area places.
- Use as many free sources as possible; keep OpenSky (user accepts its terms for personal use);
  adsb.lol polled continuously with a per-source off switch. **Paid: Flightradar24 Explorer**
  (credit-capped, never polled). ADS-B Exchange only if the Sources report shows gaps.
- **FAA SWIM** approved, including the Solace client library.
- **Own receiver** planned: Raspberry Pi 2 near the antenna on Ethernet; use its IP, not `pi.local`.
- Simple mode: Simple is the default view when a filter fits; no "Visible from place" row; two
  conditions on one field stay in Advanced.
- Military suggestions: no registration-pattern suggestions; minimum marks is a setting;
  dismissed stay dismissed unless the opt-in "evidence doubled" setting is on.
- The user approved merging and deploying finished work once all checks pass.
- Repo goes public (MIT) when they choose. Never commit their domain, coordinates, tokens, ntfy
  details or real places.

## Waiting on the user

First-run setup, then ntfy connection and test · API keys: FlyItalyADSB, OpenSky · FR24
subscription · FAA SWIM registration (TFMS → TFMData, see `docs/SOURCES.md`) · receiver hardware
and feeder accounts · airplanes.live access (403 to us; feeding it or emailing them is the way in).

## Open questions for the user

- FR24 gap-fill: also for aircraft that matched a filter (can be costly with broad filters), or
  only followed aircraft? Should FR24 route data become filter fields? Add "Look up on FR24" to
  the watch status for lost followed aircraft?
- Simple mode: a "within N min" lead time per filter (needs a filter-level setting in the cascade)?
  An "any active airport/area" place option (needs a new engine sentinel next to `ANY_VIEWPOINT`)?
- SWIM: alerts for filed flights not yet in ADS-B range ("C-17 filed into the airport at 14:20")?
  Filters only see tracked aircraft today, so this needs a design decision.

## Suggested next work

1. Look at the new UI in a browser (simple mode on a phone, military suggestions, Sources report,
   FR24 lookup card, filed flights list).
2. After setup: watch the logs for source health, FAA import, hexdb.io and filter evaluation ms.
3. Tune prediction on ≥2 weeks of real recordings (`python -m app.replay`); none exist yet.

## Gotchas

- **Two pollers on one IP** double the load on adsb.fi, which IP-blocks after repeated 4xx. Run
  dev copies with `RUN_BACKGROUND=false`, or stop production while testing polling.
- Dev server: `ENV=development DATA_DIR=./.devdata uv run python -m app.main`. Set `SETUP_TOKEN` for a
  known first-run token. TOTP codes can't be reused within 30 s (matters for browser tests).
- After changing prediction code, run the scenarios across many seeds and compare fixture metrics
  (`REPLAY_DUMP=… pytest tests/test_replay.py`); rules in `CLAUDE.md` → Testing.
- Datasets download into `/data` at runtime; never commit them. adsbdb and FR24 data must never be
  exported or committed. The lock file is generated with the uv version the Dockerfile uses.
