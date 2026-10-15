import asyncio
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app import USER_AGENT, __version__
from app.auth.deps import Ctx, CurrentUser
from app.enrich.photos import planespotters_photo
from app.filters.facts import LiveFacts
from app.filters.fields import FIELDS
from app.runtime import aircraft_summary

router = APIRouter(prefix="/api", tags=["live"])

# Cloudflare drops responses that are idle for ~100 s.
HEARTBEAT_S = 15.0


@router.get("/aircraft")
async def aircraft(ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    rt = ctx.rt()
    ectx = rt.eval_context()
    home = rt.home()
    return {
        "now": ectx.now,
        "home_id": home.id if home else None,
        "aircraft": rt.summaries(rt.facts_for_all(ectx)),
    }


@router.get("/aircraft/{hex_}")
async def aircraft_detail(hex_: str, ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    rt = ctx.rt()
    track = rt.store.get(hex_.lower()[:10])
    if track is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not in range right now")
    ectx = rt.eval_context()
    facts = LiveFacts(track, ectx)
    general = {k: facts.get(k) for k, fd in FIELDS.items() if not fd.place}
    per_place = []
    for p in rt.active_places():
        values = {
            k: facts.get(k, p.id)
            for k, fd in FIELDS.items()
            if fd.place and (not fd.place_roles or p.role in fd.place_roles)
        }
        per_place.append({"place_id": p.id, "name": p.name, "role": p.role, "facts": values})
    info = track.info
    pa = info.plane_alert if info else None
    cls = track.classification
    return {
        "summary": aircraft_summary(facts, rt.home(), rt.matches.get(track.hex, [])),
        "facts": general,
        "places": per_place,
        "military_reasons": list(cls.reasons) if cls else [],
        "plane_alert": (
            {
                "operator": pa.operator,
                "type": pa.type_desc,
                "category": pa.category,
                "tags": list(pa.tags),
                "cmpg": pa.cmpg,
                "link": pa.link,
                "images": list(pa.images),
            }
            if pa
            else None
        ),
        "history": [[p.t, p.lat, p.lon, p.alt_ft] for p in track.history],
        "first_seen": track.first_seen,
    }


@router.get("/photos/{hex_}")
async def photo(hex_: str, ctx: Ctx, _: CurrentUser) -> dict[str, Any] | None:
    hex_ = hex_.lower()[:10]
    rt = ctx.rt()
    track = rt.store.get(hex_)
    info = track.info if track and track.info else await rt.enricher.lookup(hex_)
    pa = info.plane_alert
    if pa and pa.images:
        return {"source": "plane-alert-db", "src": pa.images[0], "link": pa.link, "credit": None}
    ua = f"{USER_AGENT.rstrip(')')}; +{ctx.settings.contact_url})"
    return await planespotters_photo(ctx.db, ctx.http, ctx.clock, hex_, ua)


@router.get("/status")
async def status_(ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    rt = ctx.rt()
    now = ctx.clock.now()
    return {
        "version": __version__,
        "now": now,
        "tracks": len(rt.store),
        "places_active": len(rt.active_places()),
        "circles": [c.__dict__ for c in rt.circles()],
        "sources": rt.poller.status(),
        "datasets": await rt.datasets.status(),
        "dataset_busy": rt.datasets.busy,
        "recordings": rt.recorder.usage(),
        "coverage": rt.coverage.report(),
        "notifications_today": await rt.alerts.sent_today(now),
        "daily_ceiling": rt.alerts.settings.daily_ceiling,
        "ceiling_hit_at": rt.alerts.ceiling_hit_at,
        "last_eval_ms": round(rt.last_eval_ms, 1),
        "last_eval_at": rt.last_eval_at,
        "live_clients": rt.broker.clients,
        "ha_events": {"connected": rt.ha_events.connected, "last_error": rt.ha_events.last_error},
        "routes": {
            "cached": len(rt.routes.routes),
            "pending": len(rt.routes.pending),
            "requests": rt.routes.requests,
            "last_error": rt.routes.last_error,
        },
        "background": ctx.settings.run_background,
    }


@router.post("/datasets/refresh")
async def refresh_datasets(ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    rt = ctx.rt()
    if rt.datasets.busy:
        return {"started": False, "busy": rt.datasets.busy}

    async def run() -> None:
        if await rt.datasets.refresh_due(force=True):
            await rt.refresh_enrichment()

    task = asyncio.create_task(run())
    ctx.extras.setdefault("tasks", set()).add(task)
    task.add_done_callback(ctx.extras["tasks"].discard)
    return {"started": True}


@router.get("/stream")
async def stream(request: Request, ctx: Ctx, _: CurrentUser) -> StreamingResponse:
    rt = ctx.rt()
    try:
        queue = rt.broker.subscribe()
    except RuntimeError as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(e)) from e

    async def events() -> AsyncIterator[str]:
        try:
            ectx = rt.eval_context()
            first = {"now": ectx.now, "aircraft": rt.summaries(rt.facts_for_all(ectx))}
            yield rt_event("aircraft", first)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            rt.broker.unsubscribe(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def rt_event(event: str, data: Any) -> str:
    import json

    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
