from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.deps import Ctx, CurrentUser
from app.classify.military import ClassifierSettings
from app.runtime import aircraft_summary

router = APIRouter(prefix="/api/military", tags=["military"])

REVIEW_MIN_SCORE = 0.3


class OverrideIn(BaseModel):
    key: str = Field(pattern=r"^(hex:[0-9a-f~]{1,10}|reg:[A-Z0-9-]{1,12})$")
    is_military: bool
    note: str = Field("", max_length=200)


@router.get("/settings")
async def get_settings(ctx: Ctx, _: CurrentUser) -> ClassifierSettings:
    return ctx.rt().classifier


@router.put("/settings")
async def put_settings(body: ClassifierSettings, ctx: Ctx, user: CurrentUser) -> ClassifierSettings:
    await ctx.db.set_json("classifier", body.model_dump())
    await ctx.db.audit(ctx.clock.now(), user.username, "classifier.update")
    await ctx.rt().reload_classifier()
    return ctx.rt().classifier


@router.delete("/settings")
async def reset_settings(ctx: Ctx, user: CurrentUser) -> ClassifierSettings:
    await ctx.db.execute("DELETE FROM settings WHERE key = 'classifier'")
    await ctx.db.audit(ctx.clock.now(), user.username, "classifier.reset")
    await ctx.rt().reload_classifier()
    return ctx.rt().classifier


@router.get("/overrides")
async def list_overrides(ctx: Ctx, _: CurrentUser) -> list[dict[str, Any]]:
    rows = await ctx.db.fetchall("SELECT * FROM military_overrides ORDER BY created_at DESC")
    return [{**dict(r), "is_military": bool(r["is_military"])} for r in rows]


@router.post("/overrides", status_code=status.HTTP_201_CREATED)
async def set_override(body: OverrideIn, ctx: Ctx, user: CurrentUser) -> dict[str, bool]:
    await ctx.db.execute(
        "INSERT INTO military_overrides(key, is_military, note, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET is_military = excluded.is_military, note = excluded.note",
        (body.key, int(body.is_military), body.note, ctx.clock.now()),
    )
    await ctx.db.audit(ctx.clock.now(), user.username, "military.override", body.model_dump())
    await ctx.rt().reload_classifier()
    return {"ok": True}


@router.delete("/overrides/{key}")
async def delete_override(key: str, ctx: Ctx, user: CurrentUser) -> dict[str, bool]:
    if not await ctx.db.execute("DELETE FROM military_overrides WHERE key = ?", (key,)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such override")
    await ctx.db.audit(ctx.clock.now(), user.username, "military.override.delete", {"key": key})
    await ctx.rt().reload_classifier()
    return {"ok": True}


@router.get("/review")
async def review(ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    """Aircraft in range that are military, borderline, or overridden, with reasons."""
    rt = ctx.rt()
    ectx = rt.eval_context()
    home = rt.home()
    items = []
    for f in rt.facts_for_all(ectx):
        cls = f.track.classification
        if cls is None or (cls.military_score < REVIEW_MIN_SCORE and cls.override is None):
            continue
        items.append(
            {
                **aircraft_summary(f, home, rt.matches.get(f.hex, [])),
                "reasons": list(cls.reasons),
                "signals": sorted(cls.signals),
                "override": cls.override,
            }
        )
    items.sort(key=lambda a: -(a["military_score"] or 0))
    return {"threshold": rt.classifier.threshold, "aircraft": items}
