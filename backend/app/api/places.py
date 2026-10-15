from fastapi import APIRouter, HTTPException, Query, status

from app.auth.deps import Ctx, CurrentUser
from app.places.models import Place, PlaceIn
from app.places.repo import PlaceRepo

router = APIRouter(prefix="/api/places", tags=["places"])

USGS_EPQS = "https://epqs.nationalmap.gov/v1/json"


async def _changed(ctx: Ctx) -> None:
    if ctx.runtime is not None:
        await ctx.runtime.reload_places()


@router.get("")
async def list_places(ctx: Ctx, _: CurrentUser) -> list[Place]:
    return await PlaceRepo(ctx.db, ctx.clock).list()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_place(body: PlaceIn, ctx: Ctx, user: CurrentUser) -> Place:
    place = await PlaceRepo(ctx.db, ctx.clock).create(body)
    await ctx.db.audit(ctx.clock.now(), user.username, "place.create", {"id": place.id})
    await _changed(ctx)
    return place


@router.put("/{place_id}")
async def update_place(place_id: int, body: PlaceIn, ctx: Ctx, user: CurrentUser) -> Place:
    place = await PlaceRepo(ctx.db, ctx.clock).update(place_id, body)
    if place is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such place")
    await ctx.db.audit(ctx.clock.now(), user.username, "place.update", {"id": place_id})
    await _changed(ctx)
    return place


@router.delete("/{place_id}")
async def delete_place(place_id: int, ctx: Ctx, user: CurrentUser) -> dict[str, bool]:
    if not await PlaceRepo(ctx.db, ctx.clock).delete(place_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such place")
    await ctx.db.audit(ctx.clock.now(), user.username, "place.delete", {"id": place_id})
    await _changed(ctx)
    return {"ok": True}


@router.get("/elevation")
async def lookup_elevation(
    ctx: Ctx,
    _: CurrentUser,
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
) -> dict[str, float | None]:
    """Ground elevation from the USGS point query service (US only)."""
    try:
        resp = await ctx.http.get(
            USGS_EPQS,
            params={"x": lon, "y": lat, "units": "Feet", "wkid": 4326},
            timeout=10,
        )
        value = float(resp.json()["value"])
    except Exception:
        return {"elevation_ft": None}
    # The service reports large negative sentinels outside its coverage.
    return {"elevation_ft": round(value) if value > -1000 else None}
