# CLAUDE.md

Personal ADS-B aircraft alert service. It polls free ADS-B aggregators (plus optional paid APIs) around the user's active places, enriches and classifies aircraft (with a strong focus on military), evaluates user-defined watch rules, and predicts which aircraft will be visible from a viewpoint, arrive at an airport target, or enter an area target. It pushes notifications to Android through Home Assistant. It ships as **one lightweight Docker container** on a shared home server, with a PWA web UI exposed on the public internet behind built-in auth with TOTP 2FA.

**Status:** planning. The full design, data-source research and phase order are in **`docs/PLAN.md`**; read it before starting any phase. Don't start a phase the user hasn't approved. When the design changes, update `docs/PLAN.md` in the same commit.

## Stack (planned)

- Backend: Python 3.12, FastAPI, uvicorn (**single worker**, asyncio), httpx, pydantic v2, aiosqlite (SQLite WAL), numpy (prediction ensemble), shapely (polygon area targets), argon2-cffi, pyotp.
- Frontend: Svelte + Vite SPA, Leaflet, SSE for live data, PWA manifest + service worker. Built to static files and served by FastAPI.
- Tooling: `uv`, `ruff`, `mypy --strict`, `pytest`; `npm`, `svelte-check`.
- Container: multi-stage Dockerfile, non-root, `/data` volume, `mem_limit: 256m`.

## Commands

The project doesn't exist yet; Phase 0 creates these. Keep this section in sync once it does.

```bash
cd backend && uv sync                          # install
uv run ruff check . && uv run ruff format --check .
uv run mypy app
uv run pytest                                  # unit + scenario + replay-fixture tests
uv run python -m app.replay <recording-dir> --zone home   # prediction metrics on recorded data
cd frontend && npm ci && npm run check && npm run build
docker compose up --build
```

Before every commit: lint, typecheck and tests must pass for any side you touched.

## Architecture map

`backend/app/`:

- `sources/` — pluggable live sources behind the `Source` protocol (adsb.fi primary, adsb.lol failover; optional OpenSky/FR24 later). They **only fetch and normalize**. Rate limiting, retries and circuit breaking live in `ingest/`.
- `ingest/` — query planner (places → inner/outer query circles, merged), poller, token-bucket rate limiter, merge-by-hex, recorder.
- `enrich/` — tar1090-db aircraft DB + military address ranges, VRS standing-data airlines and routes, adsbdb fallback, FlightAware AeroAPI filed plans (optional, budget-capped), OurAirports airports and runways. All lookups go through the SQLite `cache` table.
- `classify/` — military/government classifier: weighted signals → score + human-readable reasons; user overrides always win. Also the local-pattern-traffic detector.
- `tracks/` — in-memory per-aircraft ring buffers and derived kinematics.
- `places/` — viewpoints (visibility: elevation angle + size-based slant range), airport targets (ICAO + runways), area targets (polygon + altitude band + schedule).
- `predict/` — local-tangent-plane geometry, CPA/ETA, arrival score and runway-approach projection, two-stage (gate + numpy ensemble) prediction, confidence score.
- `rules/` — pydantic rule schema and the matching engine.
- `alerts/` — episode state machine (`predicted → nearby → departed/expired`), dedup, cooldowns, message formatting.
- `notify/` — `Notifier` interface; Home Assistant (primary), ntfy, web push.
- `api/` — REST + SSE. `replay/` — offline replay harness, metrics, and the what-if engine behind the Tuning simulator.

## Conventions

- **Units everywhere inside the backend:** nautical miles, knots, feet, feet/min, degrees true (0–360), WGS84 lat/lon, UTC epoch seconds as `float`. Convert only at the UI and notification edges. Suffix names with units when ambiguous (`radius_nm`, `alt_ft`, `eta_s`).
- **Never call wall-clock time directly** in ingest, predict, rules or alerts. Use the injected `Clock` so replay and tests are deterministic.
- ICAO hex is stored **lowercase**. Callsigns are stripped and uppercased. Type designators are uppercased.
- Pure functions for geometry, kinematics, prediction and rule matching, so they're testable without I/O. Side effects stay in `ingest/`, `notify/`, `db/`, `api/`.
- Use the pydantic models in `rules/models.py` and `places/models.py` in API, engine and DB. Don't duplicate the shapes.
- Terminology: **place** = viewpoint | airport target | area target. Don't reintroduce "zone".
- Every setting that affects alerting (lead time, confidence, visibility, cooldowns) cascades **global → place → rule**. New settings follow the same cascade and must be usable in the Tuning simulator.
- Schema changes go through numbered SQL migrations. Never edit an applied migration.
- Keep comments sparse. Explain *why* (e.g. a threshold's source), not *what*.

## Data-source etiquette (hard rules)

- **Never pull global feeds.** Only point/radius queries from the planner, max 250 nm.
- Stay at **≤50% of each source's published rate limit** (adsb.fi: 1 req/s ⇒ budget 0.5 req/s). adsb.lol has no published limit, so treat it the same way.
- adsb.fi temporarily **IP-blocks after repeated 4xx**. Back off on any 4xx/429 (circuit breaker, exponential up to 5 min). Never hot-loop retries.
- Send gzip `Accept-Encoding` and a descriptive `User-Agent` (`airplane-tracker/<version> (personal, non-commercial)`).
- Attribution must stay visible in the UI: adsb.fi (link required), adsb.lol (ODbL), OpenStreetMap tiles, OurAirports.
- airplanes.live returned 403 "contact us" on 2026-10-14. Don't add it without the user's approval from them.
- adsbdb route data must not be redistributed: cache it privately and never include it in exports or fixtures.
- **Paid APIs** (FlightAware AeroAPI etc.) are opt-in, keyed per user, and always go through the `api_budget` monthly cap (default: the free allowance). Only look up rule candidates, never every aircraft, and cache results.
- Don't commit downloaded datasets (tar1090-db, standing-data, OurAirports). They download at runtime into `/data`.

## Lightweight constraints

- No new services (Redis, Postgres, brokers, separate workers) and no heavy dependencies without asking the user first.
- Keep one process. Background work runs as asyncio tasks started in the FastAPI lifespan.
- Everything in memory is bounded: ring buffers, caches, SSE client queues. Disk data has retention (recordings 14 days by default).

## Security

- Every route requires a session except `/healthz`, `/login`, the manifest and icons. With `AUTH_MODE=proxy`, trust the user header only from configured proxy IPs.
- Secrets (HA token, ntfy credentials, VAPID keys, password hash) come from env vars or the `/data` volume. They never go in the repo, logs, recordings or the snapshot JSON.
- Validate user-provided regexes (callsign patterns) with a length limit and compile timeout, so a bad pattern can't stall the evaluation loop.
- Internet-facing: keep TOTP, login lockout, CSRF (Origin check + token), CSP/HSTS headers, and `/docs` disabled in production. Read the client IP only from trusted-proxy headers.
- Notification payloads and anything else sent off-box must not contain viewpoint (home) coordinates.

## Testing

- Prediction changes **must** include synthetic scenario tests (`tests/scenarios/`), and should report replay metrics before and after: precision, recall, false alarms/day, lead time, ETA error, calibration. Don't loosen a scenario's expected outcome to make a change pass. Explain any metric regression in the commit message.
- Replay fixtures in `tests/fixtures/replay/` stay small (<1 MB each) and come with an expected-alerts file.
- Network calls in tests are mocked (`respx`). Tests never hit real APIs.

## Public-repo hygiene

The repo is intended to go public (MIT) after the Phase 1 MVP, so treat it as public now:

- Never commit personal data: home coordinates, the user's domain, HA URLs or tokens, real places or rules. These live in `.env` and `/data`. Ship generic examples only.
- Never commit third-party data except synthetic fixtures or attributed adsb.lol (ODbL) excerpts. Never commit adsb.fi, adsbdb or FlightAware data.
- gitleaks runs in CI. Git history must stay clean, because history becomes public too.

## Git

- Small, focused commits with descriptive messages. One phase or feature per PR.
- Don't commit `/data`, recordings (except curated fixtures), `.env`, or built frontend assets.
