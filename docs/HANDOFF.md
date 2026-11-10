# Handoff (2026-11-10)

For whoever (human or LLM) continues this project. Read `CLAUDE.md` first (rules and architecture
map), then this file. Deeper docs: `docs/PLAN.md` (design and history), `docs/SOURCES.md` (data
sources and sign-ups), `docs/NTFY.md`, `docs/DEPLOY.md`. Delete or update this file when it stops
being true.

## Where we are

Personal ADS-B alert service: polls adsb.fi (adsb.lol failover) around the user's places,
enriches/classifies aircraft (military focus), runs user-built filters, and pushes matches to the
phone through the user's own **ntfy** server. One Docker container; PWA web UI with a live map.

- **Built and merged to `main`:** Phases 0–3 and most of 4 (auth+TOTP, ingest, enrichment, military
  classifier, filter engine and dashboard, places/airports/areas, prediction with ETA, replay
  harness, "What if?" backtest, sightings history, day/night, FAA airspace search, backup/restore),
  plus ntfy notifications, **follow specific aircraft anywhere** (`app/watch.py`) and
  **take-off/landing notifications** (`app/tracks/events.py`).
- **Last verified 2026-10-31:** 151 backend tests, ruff and `mypy --strict` clean, frontend checks
  and build clean, and the image built, started healthy and used ~97 MB of its 256 MB limit.
- **Deployment:** on 2026-11-01 the user reworked it to use the server's *shared* Cloudflare tunnel:
  the app publishes `127.0.0.1:8790` (`HOST_PORT`) and the tunnel routes `adsb.<domain>` to it
  (`docs/DEPLOY.md`). **I don't know whether it is running, or whether ntfy is set up yet.**
  Check before assuming either.
- Home Assistant was removed at the user's request. Never built: military review suggestions,
  multi-source merge, other sources, simple filter mode (below).

## Not yet verified against real services

- **ntfy** (payloads follow ntfy's docs; mock-tested only). First real check: Settings → "Save &
  send test notification". Needs ntfy ≥ 2.16 for in-place updates (`sequence_id`).
  `docs/NTFY.md` still shows ntfy with its own cloudflared container; with the shared tunnel it
  probably needs a route to ntfy's port instead. Update it once the real setup is known.
- **Take-off/landing detection** on live traffic (tested on synthetic flights only). Watch for false
  "landed (probably)"; thresholds are constants at the top of `events.py`.
- **Notification buttons** need `PUBLIC_URL` set and reachable from the phone.

## User decisions

- ntfy only (self-hosted, own hostname); no Home Assistant, no Web Push, **no container registry
  publishing**.
- Use as many free sources as possible. **Paid: Flightradar24 Explorer approved** (~$9/month flat,
  limited by *credits*: enforce a monthly credit cap, on-demand lookups only). ADS-B Exchange only if
  the Sources report shows gaps. Details in `docs/SOURCES.md`.
- **FAA SWIM** approved, including the Solace client library; the user must register first.
- **Own receiver** planned: their Raspberry Pi 2 near the antenna on Ethernet, feeding all networks;
  the app will read its `aircraft.json` over the LAN. Server is in the basement.
- Wants an **FR24-style "simple mode"** filter builder (flat list of common filters) alongside the
  full builder, and **military review suggestions** derived from their overrides.
- Repo goes public (MIT) when they choose. Never commit their domain, coordinates, tokens, ntfy
  details or real places.

## Waiting on the user

Deploy/verify the app and ntfy · API keys: FlyItalyADSB, OpenSky · FAA SWIM registration · FR24
subscription · receiver hardware and feeder accounts · airplanes.live access (it returned 403 to us;
feeding it or emailing them is the way in).

## Suggested next work

1. Military review suggestions (no external dependency).
2. Multi-source merge + Sources report (poller currently fails over; merge every enabled source
   within its own budget), then FlyItalyADSB (reuses `readsb.parse_payload`) and OpenSky (OAuth2).
3. Local receiver source (readsb `aircraft.json`, polled every second).
4. FR24 gap-fill/on-demand source with a credit cap. 5. FAA SWIM schedule source.
6. Simple filter mode. 7. Tune prediction on ≥2 weeks of real recordings (`python -m app.replay`).

## Gotchas

- **Two pollers on one IP** double the load on adsb.fi, which IP-blocks after repeated 4xx. Run
  dev copies with `RUN_BACKGROUND=false`, or stop production while testing polling.
- Dev server: `ENV=development DATA_DIR=./.devdata uv run python -m app.main`. Set `SETUP_TOKEN` for a
  known first-run token. TOTP codes can't be reused within 30 s (matters for browser tests).
- After changing prediction code, run the scenarios across many seeds and compare fixture metrics
  (`REPLAY_DUMP=… pytest tests/test_replay.py`); rules in `CLAUDE.md` → Testing.
- Datasets download into `/data` at runtime; never commit them. adsbdb data must never be exported.
