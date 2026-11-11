# Handoff (2026-11-10)

For whoever (human or LLM) continues this project. Read `CLAUDE.md` first (rules and architecture
map), then this file. Deeper docs: `docs/PLAN.md` (design and history), `docs/SOURCES.md` (data
sources and sign-ups), `docs/NTFY.md`, `docs/DEPLOY.md`. Delete or update this file when it stops
being true.

## Where we are

Personal ADS-B alert service: polls every enabled source (adsb.fi, adsb.lol, optional FlyItalyADSB,
OpenSky and the user's own receiver) around the user's places, merges them by hex,
enriches/classifies aircraft (military focus), runs user-built filters, and pushes matches to the
phone through the user's own **ntfy** server. One Docker container; PWA web UI with a live map.

- **Built and merged to `main`:** Phases 0–3 and most of 4 (auth+TOTP, ingest, enrichment, military
  classifier, filter engine and dashboard, places/airports/areas, prediction with ETA, replay
  harness, "What if?" backtest, sightings history, day/night, FAA airspace search, backup/restore),
  plus ntfy notifications, **follow specific aircraft anywhere** (`app/watch.py`) and
  **take-off/landing notifications** (`app/tracks/events.py`).
- **Built 2026-11-10 (branch `claude/integration`, merging `claude/military-suggestions`,
  `claude/multi-source`, `claude/simple-filter-mode`; not yet on `main`):**
  - Military review suggestions from overrides (`classify/suggest.py`, migration 0007 adds an
    override snapshot). Accept/dismiss on the military review page; never auto-applied.
  - Multi-source merge (no more failover): each source has its own loop, token bucket (≤50%) and
    breaker; merge rules in `tracks/store.py`; filter fields `sources`, `seen_by_my_receiver`;
    Sources report on the System page (`ingest/sourcestats.py`, `GET /api/sources`).
  - Opt-in sources: FlyItalyADSB (`FLYITALY_API_KEY`), OpenSky (`OPENSKY_CLIENT_ID/SECRET`,
    credit-metered), local receiver (`RECEIVER_URL`, optional `RECEIVER_UAT_URL`).
    Prediction now runs at most every 5 s (`MIN_CYCLE_S`) so 1 Hz data doesn't change hysteresis.
    Each source can be switched off/on in the Sources report (setting `sources`).
  - Suggestion settings: minimum agreeing marks (default 3) and opt-in "bring back a dismissed
    suggestion once its evidence doubles".
  - FR24-style simple mode in the filter editor (`frontend/src/lib/simple.ts`), lossless
    round-trip to the same expression tree; route origin/destination autocomplete airports.
- **Last verified 2026-11-10 (integration branch):** 182 backend tests, ruff and `mypy --strict`
  clean; frontend check, 26 tests and build clean (host has no Node: run npm in `node:22-slim`).
  Replay fixture metrics unchanged. **No new UI has been looked at in a browser yet**, and the
  Docker image hasn't been rebuilt with these changes.
- **Deployment:** on 2026-11-01 the user reworked it to use the server's *shared* Cloudflare tunnel:
  the app publishes `127.0.0.1:8790` (`HOST_PORT`) and the tunnel routes `adsb.<domain>` to it
  (`docs/DEPLOY.md`). **Checked 2026-11-10:** the container runs healthy (~130 MB) with datasets
  imported, but **first-run setup was never completed** (0 users, 0 places), so nothing is polled,
  recorded or alerted yet. An ntfy container (v2.34.0, ≥ 2.16 so in-place updates work) runs on
  the same server; whether the app is configured to use it is unknown.
- Home Assistant was removed at the user's request.

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
- Repo goes public (MIT) when they choose. Never commit their domain, coordinates, tokens, ntfy
  details or real places.

## Waiting on the user

Finish first-run setup, then verify ntfy · review/merge the 2026-11-10 branches · API keys: FlyItalyADSB, OpenSky · FAA SWIM registration · FR24
subscription · receiver hardware and feeder accounts · airplanes.live access (it returned 403 to us;
feeding it or emailing them is the way in).

## Suggested next work

1. Look at the new UI in a browser (simple mode on a phone, military suggestions, Sources report).
2. FR24 gap-fill/on-demand source with a credit cap (needs the subscription).
3. FAA SWIM schedule source (needs registration).
4. Tune prediction on ≥2 weeks of real recordings (`python -m app.replay`); none exist yet.

## Decisions from 2026-11-10

- Keep OpenSky (the user accepts its terms for personal use). adsb.lol polled continuously, with a
  per-source off switch. Recording growth with 14-day retention is fine (check DB size after a week).
- Simple mode: default view stays Simple when the filter fits; **no** "Visible from place" row —
  the app is about what is at or headed to the airport or bombing range, not what's visible from
  home. Two conditions on one field stay in Advanced.
- Military suggestions: no registration-pattern suggestions; minimum marks is a setting; dismissed
  stay dismissed unless the opt-in "evidence doubled" setting is on; restore replaces dismissals.
- Notes: FlyItalyADSB limits come from their docs (one page says 100 req/min); the Sources report
  will show real usage. Use the Pi's IP, not `pi.local`, for `RECEIVER_URL`. Check "Filter
  evaluation" ms once 1 Hz receiver data flows.

## Gotchas

- **Two pollers on one IP** double the load on adsb.fi, which IP-blocks after repeated 4xx. Run
  dev copies with `RUN_BACKGROUND=false`, or stop production while testing polling.
- Dev server: `ENV=development DATA_DIR=./.devdata uv run python -m app.main`. Set `SETUP_TOKEN` for a
  known first-run token. TOTP codes can't be reused within 30 s (matters for browser tests).
- After changing prediction code, run the scenarios across many seeds and compare fixture metrics
  (`REPLAY_DUMP=… pytest tests/test_replay.py`); rules in `CLAUDE.md` → Testing.
- Datasets download into `/data` at runtime; never commit them. adsbdb data must never be exported.
