from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from app import USER_AGENT
from app.auth.deps import Ctx, CurrentUser
from app.sources.airspace import ATTRIBUTION, AirspaceError, AirspaceSearch

router = APIRouter(prefix="/api/airspace", tags=["airspace"])


@router.get("/search")
async def search(
    ctx: Ctx,
    _: CurrentUser,
    q: str | None = Query(None, max_length=40),
    lat: float | None = Query(None, ge=-90, le=90),
    lon: float | None = Query(None, ge=-180, le=180),
    radius_nm: float = Query(30, gt=0, le=100),
) -> dict[str, Any]:
    """Special use airspace by name (e.g. 'R-3601'), or near a point."""
    finder = AirspaceSearch(ctx.db, ctx.http, ctx.clock, USER_AGENT)
    try:
        if q and q.strip():
            found = await finder.by_name(q)
        elif lat is not None and lon is not None:
            found = await finder.near(lat, lon, radius_nm)
        else:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Give a name or a position")
    except AirspaceError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
    return {"results": [a.public() for a in found], "attribution": ATTRIBUTION}
