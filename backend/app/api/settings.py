from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.alerts.manager import AlertingSettings
from app.auth.deps import Ctx, CurrentUser
from app.context import AppContext
from app.notify.base import NotifyError
from app.notify.ntfy import NtfyClient, NtfyConfig
from app.predict.engine import PredictionSettings

router = APIRouter(prefix="/api/settings", tags=["settings"])


class NtfySettingsIn(BaseModel):
    url: str = Field(max_length=300, pattern=r"^https?://")
    topic: str = Field(min_length=1, max_length=64, pattern=r"^[-_A-Za-z0-9]+$")
    token: str | None = Field(None, max_length=200)  # None keeps the stored token
    clear_token: bool = False


async def load_ntfy_config(ctx: AppContext) -> NtfyConfig | None:
    """Env vars win over values saved in the UI."""
    stored: dict[str, Any] = await ctx.db.get_json("ntfy", {})
    url = ctx.settings.ntfy_url or stored.get("url")
    topic = ctx.settings.ntfy_topic or stored.get("topic")
    token = ctx.settings.ntfy_token or stored.get("token")
    if not url or not topic:
        return None
    return NtfyConfig(url=url, topic=topic, token=token or None)


@router.get("/ntfy")
async def get_ntfy(ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    stored: dict[str, Any] = await ctx.db.get_json("ntfy", {})
    return {
        "url": ctx.settings.ntfy_url or stored.get("url", ""),
        "topic": ctx.settings.ntfy_topic or stored.get("topic", ""),
        "token_set": bool(ctx.settings.ntfy_token or stored.get("token")),
        "public_url": ctx.settings.public_url,
        "from_env": {
            "url": bool(ctx.settings.ntfy_url),
            "topic": bool(ctx.settings.ntfy_topic),
            "token": bool(ctx.settings.ntfy_token),
        },
    }


@router.put("/ntfy")
async def put_ntfy(body: NtfySettingsIn, ctx: Ctx, user: CurrentUser) -> dict[str, bool]:
    stored: dict[str, Any] = await ctx.db.get_json("ntfy", {})
    stored["url"] = body.url.strip()
    stored["topic"] = body.topic.strip()
    if body.token:
        stored["token"] = body.token.strip()
    elif body.clear_token:
        stored.pop("token", None)
    await ctx.db.set_json("ntfy", stored)
    await ctx.db.audit(ctx.clock.now(), user.username, "settings.ntfy", {"url": stored["url"]})
    return {"ok": True}


@router.post("/ntfy/test")
async def test_ntfy(ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    """Checks the server, then sends a test notification to the topic."""
    cfg = await load_ntfy_config(ctx)
    if cfg is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Set the ntfy URL and topic first")
    client = NtfyClient(ctx.http, cfg)
    try:
        await client.health()
        await client.publish(
            {
                "title": "✈ Airplane Tracker",
                "message": "Test notification. If you can read this, alerts will reach your phone.",
                "tags": ["airplane"],
                "priority": 3,
            }
        )
    except NotifyError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


@router.get("/alerting")
async def get_alerting(ctx: Ctx, _: CurrentUser) -> AlertingSettings:
    return ctx.rt().alerts.settings


@router.put("/alerting")
async def put_alerting(body: AlertingSettings, ctx: Ctx, user: CurrentUser) -> AlertingSettings:
    try:
        ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown time zone {body.timezone}"
        ) from e
    await ctx.db.set_json("alerting", body.model_dump())
    await ctx.db.audit(ctx.clock.now(), user.username, "settings.alerting")
    await ctx.rt().reload_alerting()
    return ctx.rt().alerts.settings


@router.get("/prediction")
async def get_prediction(ctx: Ctx, _: CurrentUser) -> PredictionSettings:
    return ctx.rt().predictor.settings


@router.put("/prediction")
async def put_prediction(
    body: PredictionSettings, ctx: Ctx, user: CurrentUser
) -> PredictionSettings:
    await ctx.db.set_json("prediction", body.model_dump())
    await ctx.db.audit(ctx.clock.now(), user.username, "settings.prediction")
    await ctx.rt().reload_prediction()
    return ctx.rt().predictor.settings
