from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.deps import Ctx, CurrentUser
from app.context import AppContext
from app.filters.engine import CompiledFilter, describe
from app.filters.facts import LiveFacts
from app.filters.models import FilterDefinition, FilterIn, FilterOut
from app.filters.repo import FilterRepo
from app.replay.backtest import BacktestSpec
from app.runtime import aircraft_summary

router = APIRouter(prefix="/api/filters", tags=["filters"])

PREVIEW_LIMIT = 200


class PreviewBody(BaseModel):
    definition: FilterDefinition
    filter_id: int | None = None  # applies that filter's exclusions


class ExplainBody(BaseModel):
    definition: FilterDefinition
    hex: str = Field(max_length=10)


class FlagsBody(BaseModel):
    enabled: bool | None = None
    mode: str | None = Field(None, pattern="^(live|shadow)$")


class BacktestBody(BaseModel):
    definition: FilterDefinition
    filter_id: int | None = None  # compare with this filter's saved version
    mode: Literal["live", "shadow"] = "live"
    days: float = Field(1, gt=0, le=14)


class RevertBody(BaseModel):
    version_id: int


class ReorderBody(BaseModel):
    ids: list[int] = Field(max_length=500)


def _repo(ctx: AppContext) -> FilterRepo:
    return FilterRepo(ctx.db, ctx.clock)


async def _reload(ctx: AppContext) -> None:
    if ctx.runtime is not None:
        await ctx.runtime.reload_filters()


def _places(ctx: AppContext) -> dict[int, Any]:
    return ctx.rt().places_by_id()


async def _with_meta(ctx: AppContext, f: FilterOut) -> dict[str, Any]:
    places = _places(ctx)
    compiled = CompiledFilter(f.definition, places)
    tz = ctx.rt().alerts.tz
    day_start = datetime.fromtimestamp(ctx.clock.now(), tz).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    row = await ctx.db.fetchone(
        "SELECT count(*) AS today, sum(notified) AS notified_today FROM alerts "
        "WHERE filter_id = ? AND started_at >= ?",
        (f.id, day_start.timestamp()),
    )
    last = await ctx.db.fetchone("SELECT max(started_at) FROM alerts WHERE filter_id = ?", (f.id,))
    month = await ctx.db.fetchone(
        "SELECT count(*) AS n, sum(notified) AS notified, sum(feedback = 'up') AS up, "
        "sum(feedback = 'down') AS down FROM alerts WHERE filter_id = ? AND started_at >= ?",
        (f.id, ctx.clock.now() - 30 * 86400),
    )
    return {
        **f.model_dump(),
        "summary": describe(f.definition, places),
        "errors": compiled.errors,
        "matching_now": sum(1 for ids in ctx.rt().matches.values() if f.id in ids),
        "alerts_today": row["today"] if row else 0,
        "notified_today": (row["notified_today"] or 0) if row else 0,
        "last_alert_at": last[0] if last else None,
        "stats_30d": {
            "alerts": month["n"] if month else 0,
            "notified": (month["notified"] or 0) if month else 0,
            "up": (month["up"] or 0) if month else 0,
            "down": (month["down"] or 0) if month else 0,
        },
    }


@router.get("")
async def list_filters(ctx: Ctx, _: CurrentUser) -> list[dict[str, Any]]:
    return [await _with_meta(ctx, f) for f in await _repo(ctx).all()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_filter(body: FilterIn, ctx: Ctx, user: CurrentUser) -> dict[str, Any]:
    f = await _repo(ctx).create(body)
    await ctx.db.audit(ctx.clock.now(), user.username, "filter.create", {"id": f.id})
    await _reload(ctx)
    return await _with_meta(ctx, f)


@router.post("/preview")
async def preview(body: PreviewBody, ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    from app.filters.exclusions import is_excluded

    rt = ctx.rt()
    places = rt.places_by_id()
    compiled = CompiledFilter(body.definition, places)
    ectx = rt.eval_context()
    facts = rt.facts_for_all(ectx)
    matching: list[LiveFacts] = []
    excluded = 0
    if compiled.valid:
        for f in facts:
            if compiled.matches(f):
                if body.filter_id is not None and is_excluded(
                    rt.exclusions, body.filter_id, f, ectx.now
                ):
                    excluded += 1
                else:
                    matching.append(f)
    home = rt.home()
    display = places.get(body.definition.display_place_id or -1) or home
    matching.sort(key=lambda f: (f.get("distance_nm", display.id) if display else None) or 1e9)
    return {
        "summary": describe(body.definition, places),
        "errors": compiled.errors,
        "in_range": len(facts),
        "match_count": len(matching),
        "excluded": excluded,
        "matches": [
            aircraft_summary(f, display, rt.matches.get(f.hex, []))
            for f in matching[:PREVIEW_LIMIT]
        ],
        "funnel": compiled.funnel(list(facts)) if not compiled.errors else [],
    }


@router.post("/explain")
async def explain(body: ExplainBody, ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    rt = ctx.rt()
    track = rt.store.get(body.hex.lower())
    if track is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That aircraft is no longer in range")
    compiled = CompiledFilter(body.definition, rt.places_by_id())
    facts = LiveFacts(track, rt.eval_context())
    return {
        "aircraft": aircraft_summary(facts, rt.home(), rt.matches.get(track.hex, [])),
        "errors": compiled.errors,
        "matches": compiled.matches(facts),
        "explanation": compiled.explain(facts),
    }


@router.post("/reorder")
async def reorder(body: ReorderBody, ctx: Ctx, _: CurrentUser) -> dict[str, bool]:
    await _repo(ctx).reorder(body.ids)
    await _reload(ctx)
    return {"ok": True}


@router.get("/export")
async def export_filters(ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    filters = await _repo(ctx).all()
    return {
        "format": "airplane-tracker-filters/1",
        "filters": [
            {"name": f.name, "description": f.description, "enabled": f.enabled, "mode": f.mode,
             "definition": f.definition.model_dump()}
            for f in filters
        ],
    }  # fmt: skip


class ImportBody(BaseModel):
    filters: list[FilterIn] = Field(max_length=200)


@router.post("/import")
async def import_filters(body: ImportBody, ctx: Ctx, user: CurrentUser) -> dict[str, int]:
    repo = _repo(ctx)
    for f in body.filters:
        # Imported filters start in shadow mode, so nothing pings the phone until reviewed.
        await repo.create(f.model_copy(update={"mode": "shadow", "note": "Imported"}))
    await ctx.db.audit(
        ctx.clock.now(), user.username, "filter.import", {"count": len(body.filters)}
    )
    await _reload(ctx)
    return {"imported": len(body.filters)}


@router.post("/backtest", status_code=status.HTTP_202_ACCEPTED)
async def start_backtest(body: BacktestBody, ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    """Replays recorded traffic through the draft (and the saved version, to compare)."""
    rt = ctx.rt()
    saved = None
    mode = body.mode
    if body.filter_id is not None:
        f = await FilterRepo(ctx.db, ctx.clock).get(body.filter_id)
        if f is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "filter not found")
        saved = f.definition
    end = ctx.clock.now()
    spec = BacktestSpec(
        body.definition, saved, body.filter_id or 0, mode, end - body.days * 86400, end
    )
    return rt.backtester.start(spec).public() | {"recordings": rt.recorder.usage()}


@router.get("/backtest/{job_id}")
async def get_backtest(job_id: str, ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    job = ctx.rt().backtester.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "backtest not found")
    return job.public()


@router.delete("/backtest/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_backtest(job_id: str, ctx: Ctx, _: CurrentUser) -> None:
    ctx.rt().backtester.cancel(job_id)


@router.get("/{filter_id}")
async def get_filter(filter_id: int, ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    f = await _repo(ctx).get(filter_id)
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such filter")
    return await _with_meta(ctx, f)


@router.put("/{filter_id}")
async def update_filter(
    filter_id: int, body: FilterIn, ctx: Ctx, user: CurrentUser
) -> dict[str, Any]:
    f = await _repo(ctx).update(filter_id, body)
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such filter")
    await ctx.db.audit(ctx.clock.now(), user.username, "filter.update", {"id": filter_id})
    await _reload(ctx)
    return await _with_meta(ctx, f)


@router.patch("/{filter_id}")
async def patch_filter(
    filter_id: int, body: FlagsBody, ctx: Ctx, user: CurrentUser
) -> dict[str, Any]:
    f = await _repo(ctx).set_flags(filter_id, body.enabled, body.mode)
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such filter")
    await ctx.db.audit(
        ctx.clock.now(), user.username, "filter.flags", {"id": filter_id, **body.model_dump()}
    )
    await _reload(ctx)
    return await _with_meta(ctx, f)


@router.delete("/{filter_id}")
async def delete_filter(filter_id: int, ctx: Ctx, user: CurrentUser) -> dict[str, bool]:
    if not await _repo(ctx).delete(filter_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such filter")
    await ctx.db.audit(ctx.clock.now(), user.username, "filter.delete", {"id": filter_id})
    await _reload(ctx)
    return {"ok": True}


@router.post("/{filter_id}/duplicate", status_code=status.HTTP_201_CREATED)
async def duplicate_filter(filter_id: int, ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    f = await _repo(ctx).get(filter_id)
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such filter")
    copy = await _repo(ctx).create(
        FilterIn(
            name=f"{f.name} (copy)"[:80],
            description=f.description,
            enabled=f.enabled,
            mode="shadow",
            definition=f.definition,
            note=f"Copied from {f.name}",
        )
    )
    await _reload(ctx)
    return await _with_meta(ctx, copy)


@router.get("/{filter_id}/versions")
async def versions(filter_id: int, ctx: Ctx, _: CurrentUser) -> list[dict[str, Any]]:
    places = _places(ctx)
    out = []
    for v in await _repo(ctx).versions(filter_id):
        d = FilterDefinition.model_validate(v["definition"])
        out.append({**v, "summary": describe(d, places)})
    return out


@router.post("/{filter_id}/revert")
async def revert(filter_id: int, body: RevertBody, ctx: Ctx, user: CurrentUser) -> dict[str, Any]:
    f = await _repo(ctx).revert(filter_id, body.version_id)
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such filter or version")
    await ctx.db.audit(ctx.clock.now(), user.username, "filter.revert", {"id": filter_id})
    await _reload(ctx)
    return await _with_meta(ctx, f)
