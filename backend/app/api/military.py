from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.deps import Ctx, CurrentUser
from app.classify import suggest as sg
from app.classify.military import ClassifierSettings, Inputs, classify
from app.runtime import aircraft_summary

if TYPE_CHECKING:
    from app.runtime import Runtime
    from app.tracks.store import Track

router = APIRouter(prefix="/api/military", tags=["military"])

REVIEW_MIN_SCORE = 0.3
PREFS_KEY = "military_suggestions"
# Overrides made before snapshots existed: the review page and quick actions saved the
# callsign as the note.
_CALLSIGN_NOTE = re.compile(r"^[A-Z0-9]{3,8}$")


class OverrideIn(BaseModel):
    key: str = Field(pattern=r"^(hex:[0-9a-f~]{1,10}|reg:[A-Z0-9-]{1,12})$")
    is_military: bool
    note: str = Field("", max_length=200)


class SuggestionRef(BaseModel):
    id: str = Field(min_length=1, max_length=200)


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
    rows = await ctx.db.fetchall(
        "SELECT key, is_military, note, created_at FROM military_overrides ORDER BY created_at DESC"
    )
    return [{**dict(r), "is_military": bool(r["is_military"])} for r in rows]


def _live_track(rt: Runtime, key: str) -> Track | None:
    kind, ident = key.split(":", 1)
    if kind == "hex":
        return rt.store.get(ident)
    return next(
        (t for t in rt.store.values() if t.registration and t.registration.upper() == ident), None
    )


@router.post("/overrides", status_code=status.HTTP_201_CREATED)
async def set_override(body: OverrideIn, ctx: Ctx, user: CurrentUser) -> dict[str, bool]:
    rt = ctx.rt()
    track = _live_track(rt, body.key)
    snapshot = json.dumps(sg.to_snapshot(rt.classifier_inputs(track, None))) if track else None
    await ctx.db.execute(
        "INSERT INTO military_overrides(key, is_military, note, created_at, snapshot_json) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
        "is_military = excluded.is_military, note = excluded.note, "
        "snapshot_json = COALESCE(excluded.snapshot_json, military_overrides.snapshot_json)",
        (body.key, int(body.is_military), body.note, ctx.clock.now(), snapshot),
    )
    await ctx.db.audit(ctx.clock.now(), user.username, "military.override", body.model_dump())
    await rt.reload_classifier()
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


# ---- suggestions from your overrides ----------------------------------------------------------


def _type_stats(rt: Runtime, type_code: str | None) -> tuple[int, float] | None:
    stats = rt.enricher.type_info(type_code)
    return (stats[0], stats[1]) if stats else None


async def _from_datasets(rt: Runtime, kind: str, ident: str, note: str | None) -> Inputs:
    hex_: str | None = ident if kind == "hex" else None
    if hex_ is None:
        row = await rt.ctx.db.fetchone("SELECT icao FROM aircraft_db WHERE reg = ?", (ident,))
        hex_ = row["icao"] if row else None
    info = await rt.enricher.lookup(hex_) if hex_ else None
    pa = info.plane_alert if info else None
    note_cs = (note or "").strip().upper()
    callsign = note_cs if _CALLSIGN_NOTE.match(note_cs) else None
    type_code = info.type_code if info else None
    registration = info.registration if info else None
    snap = {
        "hex": hex_ or "",
        "callsign": callsign,
        "registration": registration or (ident if kind == "reg" else None),
        "type_code": type_code,
        "owner_op": info.owner_op if info else None,
        "db_military": bool(info and info.db_military),
        "in_military_range": bool(info and info.in_military_range),
        "plane_alert_cmpg": pa.cmpg if pa else None,
        "plane_alert_operator": pa.operator if pa else None,
        "callsign_is_airline": rt.enricher.airline_for(callsign) is not None,
    }
    return sg.from_snapshot(snap, _type_stats(rt, type_code))


async def _marks(rt: Runtime) -> list[sg.Mark]:
    """Each override with the classifier inputs for its aircraft: live if it's tracked now, else
    the snapshot taken when you marked it, else the datasets."""
    rows = await rt.ctx.db.fetchall(
        "SELECT key, is_military, note, snapshot_json FROM military_overrides"
    )
    marks = []
    for r in rows:
        key: str = r["key"]
        snap = json.loads(r["snapshot_json"]) if r["snapshot_json"] else None
        track = _live_track(rt, key)
        if track is not None:
            inputs = rt.classifier_inputs(track, None)
            if inputs.callsign is None and snap and snap.get("callsign"):
                live = {**sg.to_snapshot(inputs), "callsign": snap["callsign"]}
                inputs = sg.from_snapshot(live, _type_stats(rt, inputs.type_code))
        elif snap:
            inputs = sg.from_snapshot(snap, _type_stats(rt, snap.get("type_code")))
        else:
            kind, ident = key.split(":", 1)
            inputs = await _from_datasets(rt, kind, ident, r["note"])
        marks.append(sg.Mark(key, bool(r["is_military"]), inputs))
    return marks


async def _prefs(ctx: Ctx) -> sg.SuggestionPrefs:
    return sg.SuggestionPrefs.model_validate(await ctx.db.get_json(PREFS_KEY, {}))


async def _current(ctx: Ctx) -> tuple[list[sg.Suggestion], sg.SuggestionPrefs, int]:
    rt = ctx.rt()
    prefs = await _prefs(ctx)
    marks = await _marks(rt)
    return sg.suggest(marks, rt.classifier, prefs.dismissed), prefs, len(marks)


def _live_changes(rt: Runtime, settings: ClassifierSettings) -> int:
    """Tracked aircraft whose military yes/no would flip with `settings` (overrides still win)."""
    n = 0
    for t in rt.store.values():
        if t.classification is None:
            continue
        now = classify(rt.classifier_inputs(t, rt.override_for(t)), settings).military
        n += now != t.classification.military
    return n


@router.get("/suggestions")
async def suggestions(ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    rt = ctx.rt()
    items, prefs, marks = await _current(ctx)
    return {
        "marks": marks,
        "min_evidence": sg.MIN_EVIDENCE,
        "dismissed": len(prefs.dismissed),
        "suggestions": [
            {**s.model_dump(), "live_changes": _live_changes(rt, sg.apply(rt.classifier, s))}
            for s in items
        ],
    }


@router.post("/suggestions/accept")
async def accept_suggestion(body: SuggestionRef, ctx: Ctx, user: CurrentUser) -> ClassifierSettings:
    # Recomputed here rather than trusting the client's copy, so a stale page can't apply a
    # change your current overrides no longer support.
    items, _, _ = await _current(ctx)
    sug = next((s for s in items if s.id == body.id), None)
    if sug is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This suggestion no longer applies")
    rt = ctx.rt()
    await ctx.db.set_json("classifier", sg.apply(rt.classifier, sug).model_dump())
    await ctx.db.audit(
        ctx.clock.now(), user.username, "classifier.suggestion.accept", sug.model_dump()
    )
    await rt.reload_classifier()
    return rt.classifier


@router.post("/suggestions/dismiss")
async def dismiss_suggestion(body: SuggestionRef, ctx: Ctx, user: CurrentUser) -> dict[str, int]:
    prefs = await _prefs(ctx)
    if body.id not in prefs.dismissed:
        prefs.dismissed = [*prefs.dismissed[-(sg.MAX_DISMISSED - 1) :], body.id]
        await ctx.db.set_json(PREFS_KEY, prefs.model_dump())
        await ctx.db.audit(
            ctx.clock.now(), user.username, "classifier.suggestion.dismiss", {"id": body.id}
        )
    return {"dismissed": len(prefs.dismissed)}


@router.delete("/suggestions/dismissed")
async def restore_dismissed(ctx: Ctx, user: CurrentUser) -> dict[str, int]:
    await ctx.db.set_json(PREFS_KEY, sg.SuggestionPrefs().model_dump())
    await ctx.db.audit(ctx.clock.now(), user.username, "classifier.suggestion.restore")
    return {"dismissed": 0}
