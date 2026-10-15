import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from app import USER_AGENT, __version__
from app.api import (
    airports,
    airspace,
    alerts,
    auth,
    fields,
    filters,
    live,
    military,
    places,
    settings,
)
from app.auth.service import AuthService
from app.auth.throttle import LoginThrottle
from app.clock import Clock, SystemClock
from app.config import Settings
from app.context import AppContext
from app.db import Database
from app.runtime import Runtime

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob: https:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "font-src 'self' data:; "
    "manifest-src 'self'; "
    "worker-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


def create_app(cfg: Settings | None = None, clock: Clock | None = None) -> FastAPI:
    cfg = cfg or Settings()
    clock = clock or SystemClock()
    logging.basicConfig(
        level=cfg.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(cfg.db_path)
        await db.connect()
        http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True
        )
        auth_service = AuthService(db, clock, cfg.session_days)
        await auth_service.init_setup_token(cfg.setup_token)
        ctx = AppContext(
            settings=cfg,
            db=db,
            clock=clock,
            auth=auth_service,
            throttle=LoginThrottle(),
            http=http,
            secret_key=cfg.resolved_secret_key(),
        )
        runtime = Runtime(ctx)
        ctx.runtime = runtime
        app.state.ctx = ctx
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()
            await http.aclose()
            await db.close()

    app = FastAPI(
        title="Airplane Tracker",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if cfg.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if cfg.is_production else "/openapi.json",
    )

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        h = response.headers
        h.setdefault("Content-Security-Policy", CSP)
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "geolocation=(self), camera=(), microphone=()")
        if cfg.is_production:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.url.path.startswith("/api/"):
            h.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    for module in (
        auth, settings, places, airports, airspace, filters, fields, live, alerts, military,
    ):  # fmt: skip
        app.include_router(module.router)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def api_not_found(path: str) -> JSONResponse:
        return JSONResponse({"detail": "Not found"}, status_code=404)

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa(path: str) -> Response:
        root = STATIC_DIR.resolve()
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            headers = {}
            if path.startswith("assets/"):
                headers["Cache-Control"] = "public, max-age=31536000, immutable"
            elif path in ("sw.js", "manifest.webmanifest"):
                headers["Cache-Control"] = "no-cache"
            return FileResponse(candidate, headers=headers)
        index = root / "index.html"
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"detail": "UI not built"}, status_code=404)

    return app


def run() -> None:
    import uvicorn

    uvicorn.run(
        create_app(),
        host="0.0.0.0",  # noqa: S104  (container-internal; only cloudflared reaches it)
        port=8080,
        proxy_headers=False,
        access_log=False,
    )


if __name__ == "__main__":
    run()
