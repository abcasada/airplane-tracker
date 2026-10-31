from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app import backup
from app.auth.deps import Ctx, CurrentUser

router = APIRouter(prefix="/api/backup", tags=["backup"])


@router.get("")
async def download(ctx: Ctx, user: CurrentUser) -> JSONResponse:
    """Everything you configured, as one JSON file. Contains your places (home location), but
    never credentials."""
    data = await backup.export(ctx.db, ctx.clock)
    await ctx.db.audit(ctx.clock.now(), user.username, "backup.export")
    day = datetime.fromtimestamp(ctx.clock.now(), UTC).strftime("%Y-%m-%d")
    return JSONResponse(
        data,
        headers={
            "Content-Disposition": f'attachment; filename="airplane-tracker-backup-{day}.json"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/restore")
async def restore(
    body: backup.Backup, ctx: Ctx, user: CurrentUser, settings: bool = Query(True)
) -> dict[str, Any]:
    counts = await backup.restore(ctx.db, ctx.clock, body, include_settings=settings)
    await ctx.db.audit(ctx.clock.now(), user.username, "backup.restore", counts)
    rt = ctx.rt()
    await rt.reload_places()
    await rt.reload_filters()
    await rt.reload_classifier()
    await rt.reload_prediction()
    await rt.reload_alerting()
    return counts
