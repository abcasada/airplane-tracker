# CLAUDE.md

Personal ADS-B aircraft alert service. It polls free ADS-B aggregators around the user's active places, enriches and classifies aircraft (with a strong focus on military), computes generic facts (visibility, arrival score, area entry, predictions with confidence), and runs **user-built filters** over them. It pushes matches to Android through the user's self-hosted ntfy server. It ships as **one lightweight Docker container** on a shared home server. The PWA web UI is exposed via a Cloudflare Tunnel behind built-in auth with TOTP 2FA. **The filter dashboard is the product.** The user spends their time fine-tuning filters there.

**Status:** Phases 0–3 done, Phase 4 mostly done (history, day/night, feedback stats, FAA airspace search): secure skeleton, ingest from adsb.fi/adsb.lol, enrichment datasets, military classifier, filter engine and dashboard (builder, live preview, funnel, why-not), alert episodes and ntfy notifications (signed action buttons, photos, ETA updates in place; Home Assistant was removed at the user's request); airports, routes, arrival score, travel/schedules, live map; prediction (gate + ensemble, hysteresis), replay harness with metrics, and the dashboard backtest. The full design, data-source research and phase order are in **`docs/PLAN.md`**; read it before starting any phase. **Start with `docs/HANDOFF.md`**: current state, the user's decisions, what's waiting on them, and the suggested next work. Don't start a phase the user hasn't approved. When the design changes, update `docs/PLAN.md` in the same commit.

## Stack (planned)

- Backend: Python 3.13, FastAPI, uvicorn (**single worker**, asyncio), httpx, pydantic v2, aiosqlite (SQLite WAL), numpy (prediction ensemble), shapely (polygon area targets), argon2-cffi, pyotp.
- Frontend: Svelte + Vite SPA, Leaflet, SSE for live data, PWA manifest + service worker. Built to static files and served by FastAPI.
- Tooling: `uv`, `ruff`, `mypy --strict`, `pytest`; `npm`, `svelte-check`.
- Container: multi-stage Dockerfile, non-root, `/data` volume, `mem_limit: 256m`.

## Commands

Keep this section in sync with the real commands.

```bash
cd backend && uv sync                          # install
uv run ruff check . && uv run ruff format --check .
uv run mypy app tests
uv run pytest -q                               # unit + scenario + replay-fixture tests
ENV=development DATA_DIR=./.devdata uv run python -m app.main   # dev server on :8080
DATA_DIR=./.devdata uv run python -m app.replay --days 1        # prediction metrics vs baseline from recordings
REPLAY_DUMP=/tmp/after.json uv run pytest -q tests/test_replay.py   # fixture metrics, to compare before/after
uv run python -m tests.scenarios.replay_fixture                  # regenerate the synthetic replay fixture
cd frontend && npm ci && npm run check && npm test && npm run build   # build → backend/static
npm run dev                                    # Vite on :5173, proxies /api to :8080
docker compose up -d --build                   # app + cloudflared (needs .env)
```

Before every commit: lint, typecheck and tests must pass for any side you touched.

## Architecture map

`backend/app/`:

- `sources/` — pluggable sources behind the `Source` protocol: area (adsb.fi primary, adsb.lol failover), local receiver, schedule (FAA SWIM later). `sources/airspace.py` searches FAA special use airspace for area places. Paid sources are deferred plug-ins. They **only fetch and normalize**. Rate limiting, retries and circuit breaking live in `ingest/`.
- `ingest/` — query planner (places → inner/outer query circles, merged), poller, token-bucket rate limiter, merge-by-hex, recorder.
- `watch.py` — aircraft followed anywhere: filter conditions "registration/hex is/in …" become a watchlist the poller looks up wherever they fly.
- `enrich/` — tar1090-db aircraft DB + military address ranges, plane-alert-db (ODbL), adsb.im routeset → VRS standing-data → adsbdb routes, OurAirports airports and runways. All lookups go through the SQLite `cache` table.
- `classify/` — military/government/police classifier: weighted signals → score + human-readable reasons; user overrides always win.
- `tracks/` — in-memory per-aircraft ring buffers, derived kinematics, and take-off/landing detection (`events.py`).
- `places/` — viewpoints (visibility: elevation angle + size-based slant range), airport targets (ICAO + runways), area targets (polygon + altitude band + schedule).
- `predict/` — local-tangent-plane geometry, CPA/ETA, generic arrival score and runway-approach projection, two-stage (gate + numpy ensemble) prediction, confidence score.
- `filters/` — filter expression-tree schema (pydantic), field catalog, compiler to predicates, exclusions. **One engine** serves live alerting, the dashboard live preview/funnel/"why not?", and the backtest. Never fork the evaluation logic.
- `alerts/` — episode state machine (`predicted → active → ended`), episode de-duplication, opt-in suppression, shadow mode, message templates, take-off/landing notifications per filter.
- `notify/` — `Notifier` interface; ntfy (`ntfy.py`), signed notification-button tokens (`actions.py`). Setup guide: `docs/NTFY.md`.
- `backup.py` — export/merge-restore of places, filters, mutes, settings and overrides (never credentials).
- `api/` — REST + SSE. `replay/` — offline replay (`core`), prediction metrics vs the adsb-alarm baseline (`metrics`), the dashboard backtest (`backtest`) and the `python -m app.replay` CLI.

## Conventions

- **Units everywhere inside the backend:** nautical miles, knots, feet, feet/min, degrees true (0–360), WGS84 lat/lon, UTC epoch seconds as `float`. Convert only at the UI and notification edges. Suffix names with units when ambiguous (`radius_nm`, `alt_ft`, `eta_s`).
- **Never call wall-clock time directly** in ingest, predict, filters or alerts. Use the injected `Clock` so replay and tests are deterministic.
- ICAO hex is stored **lowercase**. Callsigns are stripped and uppercased. Type designators are uppercased.
- Pure functions for geometry, kinematics, prediction and filter evaluation, so they're testable without I/O. Side effects stay in `ingest/`, `notify/`, `db/`, `api/`.
- Use the pydantic models in `filters/models.py` and `places/models.py` in API, engine and DB. Don't duplicate the shapes.
- Terminology: **place** = viewpoint | airport | area. **filter** = user-defined expression tree + places + timing + notification settings. Don't reintroduce "zone" or "rule".
- **No built-in special cases.** No airport-specific logic, no hard-coded "interesting" or "uninteresting" traffic (flight schools, pattern work, etc.). The app computes generic facts and exposes them as filter fields; the user decides. A new capability means a new field in the catalog, not a hidden behavior.
- **Suppression is opt-in.** The only always-on de-duplication is one notification per aircraft × filter × place episode, updated in place. Repeat suppression, daily caps, rate caps and quiet hours are optional and default to off. The only exception is the daily safety ceiling (default 400), a guard against runaway filters.
- Every setting that affects alerting (lead time, confidence, visibility, suppression) cascades **global → place → filter**. New settings follow the same cascade and must work in the live preview and the backtest.
- Every new filter field needs: type, units, help text, autocomplete source (if any), and a test. Stored alert `facts_json` must include it so "why did this fire?" keeps working.
- Schema changes go through numbered SQL migrations. Never edit an applied migration.
- Keep comments sparse. Explain *why* (e.g. a threshold's source), not *what*.

## Data-source etiquette (hard rules)

- **Never pull global feeds.** Only point/radius queries from the planner (max 250 nm), and per-aircraft lookups by hex/registration for filters that follow specific aircraft (`watch.py`: one request per minute for up to 40 aircraft, registration lookups ≥5 min apart). Both share each source's budget and circuit breaker.
- Stay at **≤50% of each source's published rate limit** (adsb.fi: 1 req/s ⇒ budget 0.5 req/s). adsb.lol has no published limit, so treat it the same way.
- adsb.fi temporarily **IP-blocks after repeated 4xx**. Back off on any 4xx/429 (circuit breaker, exponential up to 5 min). Never hot-loop retries.
- Send gzip `Accept-Encoding` and a descriptive `User-Agent` (`airplane-tracker/<version> (personal, non-commercial)`).
- Attribution must stay visible in the UI: adsb.fi (link required), adsb.lol (ODbL), plane-alert-db (ODbL/DbCL), OpenStreetMap tiles, OurAirports, FAA ADDS (airspace search).
- FAA ADDS special use airspace: only on user search (by name or near a point), ≥1 s between requests, cached 7 days.
- airplanes.live returned 403 "contact us" on 2026-10-14. Don't add it without the user's approval from them.
- adsbdb route data must not be redistributed: cache it privately and never include it in exports or fixtures.
- **Paid sources:** the user approved Flightradar24 (Explorer plan); others (ADS-B Exchange, FlightAware) only if they ask. Paid sources must be opt-in, keyed per user, behind a monthly credit/budget cap, never polled continuously (only filter candidates, gap-fill and on-demand lookups), and cached. See `docs/SOURCES.md`.
- adsb.im routeset: batch requests, ≤1 per 30 s, cache 24 h (misses 6 h).
- Don't commit downloaded datasets (tar1090-db, plane-alert-db, standing-data, OurAirports). They download at runtime into `/data`.
- Only borrow code, lists or heuristics from MIT/permissive projects, with attribution in `NOTICE`. Never copy GPL code (e.g. plane-notify).

## Lightweight constraints

- No new services (Redis, Postgres, brokers, separate workers) and no heavy dependencies without asking the user first.
- Keep one process. Background work runs as asyncio tasks started in the FastAPI lifespan.
- Everything in memory is bounded: ring buffers, caches, SSE client queues. Disk data has retention (recordings 14 days by default).

## Security

- Every route requires a session except `/healthz`, `/login`, the manifest, icons, and `/api/notify/action/{token}` (authorized only by its signed, expiring, single-purpose token). With `AUTH_MODE=proxy`, trust the user header only from configured proxy IPs.
- Secrets (ntfy token, API keys, password hash) come from env vars or the `/data` volume. They never go in the repo, logs, recordings or the snapshot JSON.
- Validate user-provided regexes (callsign patterns) with a length limit and compile timeout, so a bad pattern can't stall the evaluation loop.
- Internet-facing via Cloudflare Tunnel: keep TOTP, login lockout, CSRF (Origin check + token), CSP/HSTS headers, and `/docs` disabled in production. Read the client IP only from `CF-Connecting-IP` when the request comes from the trusted `cloudflared` network. The app container publishes no ports.
- SSE responses send a heartbeat at least every 20 s, because Cloudflare drops idle responses at ~100 s.
- Notification payloads and anything else sent off-box must not contain viewpoint (home) coordinates.

## Testing

- Prediction changes **must** include synthetic scenario tests (`tests/scenarios/`), and should report replay metrics before and after: precision, recall, false alarms/day, lead time, ETA error, calibration. Don't loosen a scenario's expected outcome to make a change pass. Explain any metric regression in the commit message.
- Replay fixtures in `tests/fixtures/replay/` stay small (<1 MB each) and come with an expected-alerts file.
- Network calls in tests are mocked (`respx`). Tests never hit real APIs.

## Public-repo hygiene

The repo is intended to go public (MIT) after the Phase 1 MVP, so treat it as public now:

- Never commit personal data: home coordinates, the user's domain, ntfy URLs, topics or tokens, Cloudflare tunnel tokens, real places or filters. These live in `.env` and `/data`. Ship generic examples only.
- Never commit third-party data except synthetic fixtures or attributed adsb.lol (ODbL) excerpts. Never commit adsb.fi, adsbdb or FlightAware data.
- gitleaks runs in CI. Git history must stay clean, because history becomes public too.

## Git

- Small, focused commits with descriptive messages. One phase or feature per PR.
- Don't commit `/data`, recordings (except curated fixtures), `.env`, or built frontend assets.
