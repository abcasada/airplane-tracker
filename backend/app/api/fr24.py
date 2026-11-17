"""Flightradar24 (paid): on-demand lookups from the aircraft page and FR24's own usage report.
Each lookup spends credits under the monthly cap; results are cached briefly and never exported.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.deps import Ctx, CurrentUser
from app.ingest.fr24 import FR24Service, LookupRefusedError
from app.runtime import Runtime
from app.sources.fr24 import ATTRIBUTION, ATTRIBUTION_URL

router = APIRouter(prefix="/api/fr24", tags=["fr24"])


def _attribution() -> dict[str, str]:
    return {"label": ATTRIBUTION, "url": ATTRIBUTION_URL}


def _service(rt: Runtime) -> FR24Service:
    if rt.fr24 is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Flightradar24 isn't configured")
    return rt.fr24


def _refused(e: LookupRefusedError) -> HTTPException:
    headers = {"Retry-After": str(max(1, round(e.retry_in_s)))} if e.retry_in_s else None
    code = status.HTTP_429_TOO_MANY_REQUESTS if e.retry_in_s else status.HTTP_409_CONFLICT
    return HTTPException(code, e.reason, headers=headers)


@router.get("")
async def fr24_status(ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    rt = ctx.rt()
    return {
        "configured": rt.fr24 is not None,
        "status": rt.fr24.status() if rt.fr24 is not None else None,
        "attribution": _attribution(),
    }


class LookupIn(BaseModel):
    hex: str | None = Field(None, max_length=10)
    registration: str | None = Field(None, max_length=12)
    callsign: str | None = Field(None, max_length=8)


@router.post("/lookup")
async def lookup(body: LookupIn, ctx: Ctx, user: CurrentUser) -> dict[str, Any]:
    """Where is it now, per FR24? By registration and/or callsign. FR24 can't search by hex, so
    a hex is turned into its registration (or, without one, its current callsign) first."""
    rt = ctx.rt()
    svc = _service(rt)
    reg = (body.registration or "").strip().upper() or None
    cs = (body.callsign or "").strip().upper() or None
    if body.hex and not (reg or cs):
        # The registration identifies the airframe; the callsign only when it has none.
        hex_ = body.hex.strip().lower()
        t = rt.store.get(hex_)
        reg = (t.registration if t else None) or (await rt.enricher.lookup(hex_)).registration
        cs = None if reg else (t.callsign if t else None)
    try:
        res = await svc.lookup([reg] if reg else [], [cs] if cs else [], "on_demand")
    except LookupRefusedError as e:
        raise _refused(e) from e
    await ctx.db.audit(
        ctx.clock.now(), user.username, "fr24.lookup",
        {"registration": reg, "callsign": cs, "credits": res.credits},
    )  # fmt: skip
    await rt.ingest_fr24(res)
    return {
        "query": {"registration": reg, "callsign": cs},
        "flights": [f.to_dict() for f in res.flights],
        "from_cache": res.from_cache,
        "credits": res.credits,
        "month": svc.meter.status(ctx.clock.now()),
        "attribution": _attribution(),
    }


@router.post("/usage")
async def usage(ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    """FR24's own per-endpoint credit count for the last 30 days (to calibrate the estimate)."""
    svc = _service(ctx.rt())
    try:
        rows = await svc.usage()
    except LookupRefusedError as e:
        raise _refused(e) from e
    return {"period": "30d", "endpoints": rows, "credits_per_flight": svc.credits_per_flight}
