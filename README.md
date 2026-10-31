# Airplane Tracker

A self-hosted, personal ADS-B aircraft alert service. It watches live aircraft around places you care about, using free public ADS-B APIs. It enriches and classifies them (with a strong focus on military aircraft) and runs them through **filters you build in a dashboard**. Matches go to your phone through your own [ntfy](https://ntfy.sh) server.

- **Filters are the product:** a visual builder, live match preview, and explanations of why an aircraft did or didn't match.
- **Places:** viewpoints (where you watch from, with visibility settings), airports and areas (polygons).
- **Military classifier:** explainable, combining database flags, military address blocks, type, callsign, registration and operator signals.
- **Lightweight:** one container, SQLite, no other services. Installable as a PWA, protected by login with two-factor auth.

Status: early development. See [docs/PLAN.md](docs/PLAN.md) for the design and roadmap, and [docs/DEPLOY.md](docs/DEPLOY.md) to run it.

## Development

```bash
# backend (Python 3.13, uv)
cd backend && uv sync
uv run pytest && uv run ruff check . && uv run mypy app tests
ENV=development DATA_DIR=./.devdata uv run python -m app.main   # http://localhost:8080

# frontend (Node 22)
cd frontend && npm ci
npm run dev        # http://localhost:5173, proxies /api to the backend
npm run check && npm test && npm run build   # build output goes to backend/static
```

## Data sources and attribution

Live positions come from [adsb.fi](https://adsb.fi) (Open Data API) and [adsb.lol](https://adsb.lol) (data under ODbL). Each instance polls only around its own places and stays well under each source's rate limits. Both are for personal, non-commercial use. See [NOTICE](NOTICE) for all datasets and licenses.

## License

MIT. See [LICENSE](LICENSE).
