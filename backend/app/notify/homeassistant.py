import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .base import NotifyError, OutgoingNotification

log = logging.getLogger(__name__)


class HomeAssistantError(Exception):
    pass


@dataclass(frozen=True)
class HAConfig:
    url: str
    token: str
    service: str | None

    @property
    def base(self) -> str:
        return self.url.rstrip("/")


class HomeAssistantClient:
    def __init__(self, http: httpx.AsyncClient, cfg: HAConfig) -> None:
        self.http = http
        self.cfg = cfg

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.token}", "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, json: Any = None) -> Any:
        try:
            resp = await self.http.request(
                method, f"{self.cfg.base}{path}", headers=self._headers(), json=json, timeout=10
            )
        except httpx.HTTPError as e:
            raise HomeAssistantError(f"Can't reach Home Assistant at {self.cfg.base}: {e}") from e
        if resp.status_code == 401:
            raise HomeAssistantError("Home Assistant rejected the access token (401)")
        if resp.status_code >= 400:
            raise HomeAssistantError(
                f"Home Assistant returned {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json() if resp.content else None

    async def check(self) -> str:
        data = await self._request("GET", "/api/")
        return str(data.get("message", "ok")) if isinstance(data, dict) else "ok"

    async def notify_services(self) -> list[str]:
        data = await self._request("GET", "/api/services")
        for domain in data or []:
            if domain.get("domain") == "notify":
                return sorted(
                    s for s in domain.get("services", {}) if str(s).startswith("mobile_app_")
                )
        return []

    async def notify(self, payload: dict[str, Any], service: str | None = None) -> None:
        svc = service or self.cfg.service
        if not svc:
            raise HomeAssistantError("No notify service selected")
        await self._request("POST", f"/api/services/notify/{svc}", json=payload)


ACTION_PREFIX = "ATRK_"
_IMPORTANCE = {"low": "low", "normal": "default", "high": "high", "critical": "high"}


def ha_payload(n: OutgoingNotification) -> dict[str, Any]:
    """Payload for the HA companion app on Android (iOS ignores the Android-only keys)."""
    data: dict[str, Any] = {
        "tag": n.tag,
        "group": "aircraft",
        "channel": "alarm_stream" if n.alarm_stream else n.channel,
        "importance": _IMPORTANCE[n.priority],
        "priority": "high" if n.priority in ("high", "critical") else "normal",
        "ttl": 0 if n.priority in ("high", "critical") else 3600,
        "notification_icon": "mdi:airplane",
    }
    if n.military:
        data["color"] = "#6b7a1e"
    if n.image:
        data["image"] = n.image
    if n.click_url:
        data["clickAction"] = n.click_url
    if n.actions:
        # Handled by app.notify.ha_events via HA's mobile_app_notification_action event.
        actions: list[dict[str, str]] = [
            {"action": f"{ACTION_PREFIX}MUTE:{n.hex}", "title": "Mute 1 day"},
        ]
        if n.alert_ids:
            ids = ",".join(str(i) for i in n.alert_ids)
            actions.append({"action": f"{ACTION_PREFIX}DOWN:{ids}", "title": "Not useful"})
        if n.click_url:
            actions.append({"action": "URI", "title": "Open", "uri": n.click_url})
        data["actions"] = actions
    return {"title": n.title, "message": n.message, "data": data}


class HomeAssistantNotifier:
    name = "homeassistant"

    def __init__(self, client: HomeAssistantClient) -> None:
        self.client = client

    async def send(self, n: OutgoingNotification) -> None:
        try:
            await self.client.notify(ha_payload(n))
        except HomeAssistantError as e:
            raise NotifyError(str(e)) from e
