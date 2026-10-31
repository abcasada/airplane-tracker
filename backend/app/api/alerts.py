import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from app.auth.deps import Ctx, CurrentUser
from app.filters.engine import CompiledFilter
from app.filters.exclusions import Exclusion, ExclusionIn, ExclusionRepo
from app.filters.facts import SnapshotFacts
from app.filters.models import FilterDefinition

router = APIRouter(prefix="/api", tags=["alerts"])

_LIST_COLUMNS = (
    "id, filter_id, filter_name, mode, hex, callsign, registration, type_code, type_desc, "
    "military, place_id, state, started_at, ended_at, closest_nm, notified, suppressed_reason, "
    "title, photo_url, feedback"
)


class FeedbackBody(BaseModel):
    value: Literal["up", "down"] | None


@router.get("/alerts")
async def list_alerts(
    ctx: Ctx,
    _: CurrentUser,
    filter_id: int | None = None,
    mode: Literal["live", "shadow"] | None = None,
    notified: bool | None = None,
    military: bool | None = None,
    hex: str | None = Query(None, max_length=10),
    before: float | None = None,
    since: float | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[object] = []
    for col, val in (
        ("filter_id", filter_id),
        ("mode", mode),
        ("hex", hex.lower() if hex else None),
    ):
        if val is not None:
            where.append(f"{col} = ?")
            params.append(val)
    if notified is not None:
        where.append("notified = ?")
        params.append(int(notified))
    if military is not None:
        where.append("military = ?")
        params.append(int(military))
    if before is not None:
        where.append("started_at < ?")
        params.append(before)
    if since is not None:
        where.append("started_at >= ?")
        params.append(since)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = await ctx.db.fetchall(
        f"SELECT {_LIST_COLUMNS} FROM alerts {clause} ORDER BY started_at DESC LIMIT ?",  # noqa: S608
        [*params, limit],
    )
    return [dict(r) for r in rows]


@router.get("/alerts/stats")
async def alert_stats(
    ctx: Ctx, _: CurrentUser, days: int = Query(7, ge=1, le=60)
) -> dict[str, Any]:
    since = ctx.clock.now() - days * 86400
    rows = await ctx.db.fetchall(
        "SELECT filter_id, filter_name, mode, date(started_at, 'unixepoch') AS day, "
        "count(*) AS alerts, sum(notified) AS notified FROM alerts WHERE started_at >= ? "
        "GROUP BY filter_id, day ORDER BY day",
        (since,),
    )
    return {"days": days, "rows": [dict(r) for r in rows]}


@router.get("/alerts/{alert_id}")
async def get_alert(alert_id: int, ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    row = await ctx.db.fetchone("SELECT * FROM alerts WHERE id = ?", (alert_id,))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such alert")
    alert = dict(row)
    facts = json.loads(alert.pop("facts") or "{}")
    explanation = None
    if alert["filter_version_id"]:
        v = await ctx.db.fetchone(
            "SELECT definition FROM filter_versions WHERE id = ?", (alert["filter_version_id"],)
        )
        if v:
            compiled = CompiledFilter(
                FilterDefinition.model_validate_json(v["definition"]), ctx.rt().places_by_id()
            )
            explanation = compiled.explain(SnapshotFacts(alert["hex"], facts))
    notes = await ctx.db.fetchall(
        "SELECT * FROM notifications WHERE hex = ? AND sent_at >= ? AND sent_at <= ? "
        "ORDER BY sent_at",
        (alert["hex"], alert["started_at"] - 1, (alert["ended_at"] or ctx.clock.now()) + 1),
    )
    return {
        **alert,
        "facts": facts,
        "explanation": explanation,
        "notifications": [
            {**dict(n), "alert_ids": json.loads(n["alert_ids"])}
            for n in notes
            if alert_id in json.loads(n["alert_ids"])
        ],
    }


@router.post("/alerts/{alert_id}/feedback")
async def feedback(alert_id: int, body: FeedbackBody, ctx: Ctx, _: CurrentUser) -> dict[str, bool]:
    n = await ctx.db.execute("UPDATE alerts SET feedback = ? WHERE id = ?", (body.value, alert_id))
    if not n:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such alert")
    return {"ok": True}


@router.get("/exclusions")
async def list_exclusions(ctx: Ctx, _: CurrentUser) -> list[Exclusion]:
    return await ExclusionRepo(ctx.db, ctx.clock).all()


@router.post("/exclusions", status_code=status.HTTP_201_CREATED)
async def create_exclusion(body: ExclusionIn, ctx: Ctx, user: CurrentUser) -> Exclusion:
    ex = await ExclusionRepo(ctx.db, ctx.clock).create(body)
    await ctx.db.audit(ctx.clock.now(), user.username, "exclusion.create", body.model_dump())
    await ctx.rt().reload_filters()
    return ex


@router.delete("/exclusions/{exclusion_id}")
async def delete_exclusion(exclusion_id: int, ctx: Ctx, user: CurrentUser) -> dict[str, bool]:
    if not await ExclusionRepo(ctx.db, ctx.clock).delete(exclusion_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such exclusion")
    await ctx.db.audit(ctx.clock.now(), user.username, "exclusion.delete", {"id": exclusion_id})
    await ctx.rt().reload_filters()
    return {"ok": True}
