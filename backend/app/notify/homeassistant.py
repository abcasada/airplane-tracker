import logging
from dataclasses import dataclass
from typing import Any

import httpx

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
