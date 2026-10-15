from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.alerts.manager import AlertingSettings
from app.auth.deps import Ctx, CurrentUser
from app.context import AppContext
from app.notify.homeassistant import HAConfig, HomeAssistantClient, HomeAssistantError

router = APIRouter(prefix="/api/settings", tags=["settings"])


class HASettingsIn(BaseModel):
    url: str = Field(max_length=300)
    token: str | None = Field(None, max_length=1000)  # None keeps the stored token
    service: str | None = Field(None, max_length=200)


async def load_ha_config(ctx: AppContext) -> HAConfig | None:
    """Env vars win over values saved in the UI."""
    stored: dict[str, Any] = await ctx.db.get_json("ha", {})
    url = ctx.settings.ha_url or stored.get("url")
    token = ctx.settings.ha_token or stored.get("token")
    service = ctx.settings.ha_notify_service or stored.get("service")
    if not url or not token:
        return None
    return HAConfig(url=url, token=token, service=service)


@router.get("/ha")
async def get_ha(ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    stored: dict[str, Any] = await ctx.db.get_json("ha", {})
    return {
        "url": ctx.settings.ha_url or stored.get("url", ""),
        "service": ctx.settings.ha_notify_service or stored.get("service"),
        "token_set": bool(ctx.settings.ha_token or stored.get("token")),
        "from_env": {
            "url": bool(ctx.settings.ha_url),
            "token": bool(ctx.settings.ha_token),
            "service": bool(ctx.settings.ha_notify_service),
        },
    }


@router.put("/ha")
async def put_ha(body: HASettingsIn, ctx: Ctx, user: CurrentUser) -> dict[str, bool]:
    stored: dict[str, Any] = await ctx.db.get_json("ha", {})
    stored["url"] = body.url.strip()
    if body.token:
        stored["token"] = body.token.strip()
    stored["service"] = body.service
    await ctx.db.set_json("ha", stored)
    await ctx.db.audit(ctx.clock.now(), user.username, "settings.ha", {"url": stored["url"]})
    return {"ok": True}


async def _client(ctx: AppContext) -> HomeAssistantClient:
    cfg = await load_ha_config(ctx)
    if cfg is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Set the Home Assistant URL and token first"
        )
    return HomeAssistantClient(ctx.http, cfg)


@router.post("/ha/test")
async def test_ha(ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    client = await _client(ctx)
    try:
        message = await client.check()
        services = await client.notify_services()
    except HomeAssistantError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "message": message, "services": services}


@router.post("/ha/test-notification")
async def test_notification(ctx: Ctx, _: CurrentUser) -> dict[str, Any]:
    client = await _client(ctx)
    payload = {
        "title": "✈ Airplane Tracker",
        "message": "Test notification. If you can read this, alerts will reach your phone.",
        "data": {"tag": "airplane-tracker-test", "notification_icon": "mdi:airplane"},
    }
    try:
        await client.notify(payload)
    except HomeAssistantError as e:
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
