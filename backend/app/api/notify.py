from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.auth.deps import Ctx
from app.notify import actions

router = APIRouter(prefix="/api/notify", tags=["notify"])


@router.post("/action/{token}")
async def notification_action(token: str, ctx: Ctx) -> dict[str, Any]:
    """A notification button tapped on the phone. No session: the signed token is the
    authorization, and it can only mute that one aircraft or rate those alerts."""
    if len(token) > 300:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown action")
    found = actions.verify(ctx.secret_key, token, ctx.clock.now())
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown or expired action")
    kind, value = found
    await ctx.rt().handle_notification_action(
        kind, value if kind == "mute" else [int(x) for x in value.split(",")]
    )
    return {"ok": True, "action": kind}
