from typing import Any

from fastapi import APIRouter, Query

from app.alerts.format import TEMPLATE_KEYS
from app.auth.deps import Ctx, CurrentUser
from app.filters.fields import catalog

router = APIRouter(prefix="/api/fields", tags=["fields"])

SUGGEST_LIMIT = 25


@router.get("")
async def fields(_: CurrentUser) -> dict[str, Any]:
    return {"fields": catalog(), "template_keys": TEMPLATE_KEYS}


@router.get("/suggest")
async def suggest(
    ctx: Ctx,
    _: CurrentUser,
    source: str = Query(max_length=40),
    q: str = Query("", max_length=60),
) -> list[dict[str, str]]:
    """Autocomplete values for the filter builder."""
    term = q.strip().upper()
    like = f"%{term}%"
    rt = ctx.rt()
    if source == "type_code":
        rows = await ctx.db.fetchall(
            "SELECT type_code, descr, airframes FROM type_stats "
            "WHERE type_code LIKE ? OR upper(descr) LIKE ? "
            "ORDER BY (type_code = ?) DESC, (type_code LIKE ?) DESC, airframes DESC LIMIT ?",
            (like, like, term, f"{term}%", SUGGEST_LIMIT),
        )
        return [{"value": r["type_code"], "label": r["descr"] or ""} for r in rows]
    if source == "plane_alert_category":
        rows = await ctx.db.fetchall(
            "SELECT category, count(*) AS n FROM plane_alert WHERE category IS NOT NULL "
            "AND upper(category) LIKE ? GROUP BY category ORDER BY n DESC LIMIT ?",
            (like, SUGGEST_LIMIT),
        )
        return [{"value": r["category"], "label": f"{r['n']} aircraft"} for r in rows]
    if source == "plane_alert_tag":
        rows = await ctx.db.fetchall(
            "SELECT tag, count(*) AS n FROM (SELECT tag1 AS tag FROM plane_alert UNION ALL "
            "SELECT tag2 FROM plane_alert UNION ALL SELECT tag3 FROM plane_alert) "
            "WHERE tag IS NOT NULL AND upper(tag) LIKE ? GROUP BY tag ORDER BY n DESC LIMIT ?",
            (like, SUGGEST_LIMIT),
        )
        return [{"value": r["tag"], "label": f"{r['n']} aircraft"} for r in rows]
    if source == "airline":
        rows = await ctx.db.fetchall(
            "SELECT icao, name FROM airlines WHERE icao LIKE ? OR upper(name) LIKE ? "
            "ORDER BY (icao = ?) DESC LIMIT ?",
            (like, like, term, SUGGEST_LIMIT),
        )
        return [{"value": r["icao"], "label": r["name"]} for r in rows]
    if source in ("callsign", "operator"):
        seen: dict[str, str] = {}
        ectx = rt.eval_context()
        for f in rt.facts_for_all(ectx):
            v = f.get(source)
            if v and term in str(v).upper():
                seen.setdefault(str(v), "in range now")
        if source == "operator" and len(seen) < SUGGEST_LIMIT:
            rows = await ctx.db.fetchall(
                "SELECT DISTINCT operator FROM plane_alert WHERE upper(operator) LIKE ? LIMIT ?",
                (like, SUGGEST_LIMIT),
            )
            for r in rows:
                seen.setdefault(r["operator"], "plane-alert-db")
        return [{"value": k, "label": v} for k, v in list(seen.items())[:SUGGEST_LIMIT]]
    return []
