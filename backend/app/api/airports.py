from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from app.auth.deps import Ctx, CurrentUser

router = APIRouter(prefix="/api/airports", tags=["airports"])


@router.get("/search")
async def search(
    ctx: Ctx, _: CurrentUser, q: str = Query(min_length=2, max_length=60)
) -> list[dict[str, Any]]:
    term = q.strip().upper()
    rows = await ctx.db.fetchall(
        "SELECT * FROM airports WHERE ident = ? OR icao = ? OR iata = ? "
        "OR upper(name) LIKE ? OR upper(municipality) LIKE ? "
        "ORDER BY (ident = ? OR icao = ?) DESC, "
        "CASE type WHEN 'large_airport' THEN 0 WHEN 'medium_airport' THEN 1 "
        "WHEN 'small_airport' THEN 2 ELSE 3 END, name LIMIT 20",
        (term, term, term, f"%{term}%", f"%{term}%", term, term),
    )
    return [dict(r) for r in rows]


@router.get("/{code}")
async def get_airport(code: str, ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    c = code.strip().upper()[:8]
    row = await ctx.db.fetchone(
        "SELECT * FROM airports WHERE ident = ? OR icao = ? OR iata = ? "
        "ORDER BY (ident = ?) DESC, CASE type WHEN 'large_airport' THEN 0 "
        "WHEN 'medium_airport' THEN 1 WHEN 'small_airport' THEN 2 ELSE 3 END LIMIT 1",
        (c, c, c, c),
    )
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Airport not found (the airport list downloads on first start; check System)",
        )
    runways = await ctx.db.fetchall(
        "SELECT * FROM runways WHERE airport_ident = ? ORDER BY length_ft DESC", (row["ident"],)
    )
    return {**dict(row), "runways": [dict(r) for r in runways]}
