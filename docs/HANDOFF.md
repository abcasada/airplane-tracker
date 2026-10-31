# Handoff (2026-10-31)

For the next development session (moving from a cloud sandbox to the home server). Read this, then
`CLAUDE.md` (rules), `docs/PLAN.md` (design and history), `docs/SOURCES.md` (data-source plan) and
`docs/NTFY.md` (notifications). Update or delete this file once it stops being true.

## State

All work is on branch `claude/adsb-aircraft-alerts-vtpmaj` (not yet merged to `main`); everything
is committed and pushed. Backend: 151 tests, ruff, mypy --strict clean. Frontend: svelte-check
clean, 5 vitest tests, builds. The Docker image was built and run on 2026-10-31: healthy in ~25 s,
all datasets import in ~1 min, ~97 MB RAM of the 256 MB `mem_limit`.

Built: Phases 0–3 and most of 4 (see PLAN §12), plus:
- **ntfy** is the only notifier (Home Assistant removed at the user's request): `app/notify/ntfy.py`,
  signed button tokens in `app/notify/actions.py`, public `POST /api/notify/action/{token}`.
- **Follow specific aircraft anywhere** (`app/watch.py`, PLAN §10.1a): registration/hex conditions
  become a watchlist looked up by hex every minute.
- **Take-off/landing notifications** (`app/tracks/events.py`, PLAN §10.1b), per-filter
  `on_match` / `on_takeoff` / `on_landing`.
- Backup/restore, FAA airspace search, sightings history, day/night, replay harness, backtest.

## Not yet verified against the real thing

- **ntfy:** payloads follow ntfy's publish docs and are tested with mocks only (the sandbox couldn't
  download ntfy). First real check: Settings → "Save & send test notification". Needs ntfy ≥ 2.16
  for `sequence_id` in-place updates; check whether a replaced notification re-alerts on Android.
- **Take-off/landing detection** on live traffic (logic is unit/e2e tested on synthetic flights).
  Watch for false "landed (probably)" from aircraft that drop out of coverage low near an
  airfield, and missed take-offs at fields with no low-level coverage. Thresholds are constants at
  the top of `events.py`.
- **Notification buttons** need `PUBLIC_URL` set and the phone able to reach it through Cloudflare.

## User decisions not obvious from the code

- Notifications: **ntfy only**, self-hosted on the same server with its own Cloudflare hostname
  (the user sets ntfy up; the app only needs URL/topic/token). No Home Assistant, no Web Push.
- **No container registry publishing** (no GHCR releases).
- Data sources: use as many free sources as possible; the user will get keys. **Paid: Flightradar24
  Explorer approved** (flat ~$9/month; enforce a monthly *credit* cap, on-demand lookups only, see
  SOURCES.md). ADS-B Exchange only if the Sources report shows gaps.
- **FAA SWIM:** approved, including the Solace client dependency (`solace-pubsubplus`); the user
  must register at data.faa.gov / SWIFT first (subscription: TFMS Flight Data).
- **Own receiver:** planned. Likely the user's **Raspberry Pi 2** near the antenna, on Ethernet,
  running ultrafeeder, feeding all networks; the app reads its `aircraft.json` over the LAN. Server
  is in the basement, so don't plan on long coax.
- **Military review page:** add list suggestions derived from the user's military overrides
  (approved idea, not built).
- Wants an **FR24-style "simple mode"** for building filters (flat list of common filters), keeping
  the full builder. Design sketch in PLAN §10.1a. Not built.
- Repo goes public (MIT) when the user flips it; keep hygiene rules (no domain, coordinates,
  tokens, real places in the repo).

## Waiting on the user

1. Deploy (docs/DEPLOY.md), set up ntfy (docs/NTFY.md), set `PUBLIC_URL`.
2. Keys: FlyItalyADSB (`FLYITALY_API_KEY`), OpenSky (`OPENSKY_CLIENT_ID`/`SECRET`).
3. FAA SWIM registration (`SWIM_*` settings), FR24 subscription (`FR24_API_TOKEN`).
4. Receiver hardware; feeder accounts; airplanes.live access (feeding or email, they 403'd us).

## Suggested next work (in order)

1. **Military review suggestions** (no external dependency).
2. **Multi-source merge + Sources report** (SOURCES.md "How the sources fit together"): change the
   poller from failover to merging every enabled source within its own budget; add `sources` and
   `seen_by_my_receiver` fields. Then FlyItalyADSB (ADS-B Exchange v2 format, reuses
   `readsb.parse_payload`) and OpenSky (OAuth2 client credentials, own state-vector parser).
3. **Local receiver source** (readsb `aircraft.json`, `{"aircraft": [...]}`; `parse_payload`
   already accepts it), polled every 1 s, no rate limit.
4. **FR24** on-demand/gap-fill source with a credit meter and cap (measure credits per call with
   `/api/usage`; FR24 doesn't publish them).
5. **FAA SWIM** schedule source and `filed_arrival` facts.
6. **FR24-style simple filter mode.**
7. **Tune prediction** on ≥2 weeks of real recordings with `python -m app.replay` (rules in
   CLAUDE.md "Testing": scenario tests + before/after metrics).

## Working on the server: gotchas

- **Two pollers share one IP.** A dev server next to the production container would double the
  load on adsb.fi (each instance budgets 0.5 req/s against a 1 req/s limit, and adsb.fi IP-blocks
  after repeated 4xx). Run dev instances with `RUN_BACKGROUND=false`, or with only a tiny place, or
  stop production while testing polling.
- Dev server: `ENV=development DATA_DIR=./.devdata uv run python -m app.main` (port 8080; the
  production container publishes no ports, so they don't clash). Set `SETUP_TOKEN` for a known
  first-run token.
- **TOTP replay protection:** logging in twice with the same code in one 30 s window fails. Browser
  tests must wait for the next window.
- Prediction scenarios are seeded; after changing prediction code, also run them across many seeds
  (patch `Predictor` in `tests/scenarios/test_prediction.py` with seeds 0–49) to catch flaky
  expectations. Compare fixture metrics with `REPLAY_DUMP=… pytest tests/test_replay.py`.
- Datasets download into `/data` at runtime; never commit them. adsbdb data must never be exported.
- `docs/PLAN.md` §15 "Open questions" is historical; current open items are in this file.
