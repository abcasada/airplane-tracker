"""ntfy (self-hosted push notifications) as the phone notifier."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import actions
from .base import NotifyError, OutgoingNotification

log = logging.getLogger(__name__)

# Filter priority → ntfy priority. On Android each ntfy priority has its own notification
# channel, so sound, vibration and "override Do Not Disturb" are set per level in the ntfy app.
PRIORITY = {"low": 2, "normal": 3, "high": 4, "critical": 5}


@dataclass(frozen=True)
class NtfyConfig:
    url: str  # how the app reaches ntfy, e.g. http://ntfy:80 on the Docker network
    topic: str
    token: str | None = None  # publish token (tk_…); None for an open server

    @property
    def base(self) -> str:
        return self.url.rstrip("/")


class NtfyClient:
    def __init__(self, http: httpx.AsyncClient, cfg: NtfyConfig) -> None:
        self.http = http
        self.cfg = cfg

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.cfg.token:
            h["Authorization"] = f"Bearer {self.cfg.token}"
        return h

    async def publish(self, payload: dict[str, Any]) -> None:
        body = {"topic": self.cfg.topic, **payload}
        try:
            resp = await self.http.post(
                self.cfg.base, json=body, headers=self._headers(), timeout=10
            )
        except httpx.HTTPError as e:
            raise NotifyError(f"Can't reach ntfy at {self.cfg.base}: {e}") from e
        if resp.status_code in (401, 403):
            raise NotifyError(
                f"ntfy refused the publish ({resp.status_code}): check the token and that it "
                f"may write to topic '{self.cfg.topic}'"
            )
        if resp.status_code >= 400:
            raise NotifyError(f"ntfy returned {resp.status_code}: {resp.text[:200]}")

    async def health(self) -> bool:
        try:
            resp = await self.http.get(f"{self.cfg.base}/v1/health", timeout=10)
            return resp.status_code == 200 and bool(resp.json().get("healthy"))
        except (httpx.HTTPError, ValueError) as e:
            raise NotifyError(f"Can't reach ntfy at {self.cfg.base}: {e}") from e


def ntfy_payload(
    n: OutgoingNotification,
    tz: ZoneInfo,
    action_url: Callable[[str, str], str | None],
) -> dict[str, Any]:
    message = n.message
    if n.eta_at is not None:
        # ntfy has no live countdown: an absolute time stays right as the notification ages.
        message = f"ETA {datetime.fromtimestamp(n.eta_at, tz):%H:%M}\n{message}"
    tags = ["airplane"]
    if n.military:
        tags.append("military_helmet")
    payload: dict[str, Any] = {
        "title": n.title,
        "message": message,
        "priority": 5 if n.alarm_stream else PRIORITY[n.priority],
        "tags": tags,
        # One notification per aircraft: later alerts and ETA updates replace it.
        "sequence_id": n.tag,
    }
    if n.click_url:
        payload["click"] = n.click_url
    if n.image:
        payload["attach"] = n.image
    if n.actions:
        buttons: list[dict[str, Any]] = []
        if n.click_url:
            buttons.append({"action": "view", "label": "Open", "url": n.click_url})
        if mute := action_url("mute", n.hex):
            buttons.append({"action": "http", "label": "Mute 1 day", "url": mute, "clear": True})
        if n.alert_ids and (down := action_url("down", ",".join(map(str, n.alert_ids)))):
            buttons.append({"action": "http", "label": "Not useful", "url": down, "clear": True})
        if buttons:
            payload["actions"] = buttons[:3]
    return payload


class NtfyNotifier:
    name = "ntfy"

    def __init__(
        self,
        client: NtfyClient,
        tz: ZoneInfo,
        secret: str,
        public_url: str | None,
        now: Callable[[], float],
    ) -> None:
        self.client = client
        self.tz = tz
        self.secret = secret
        self.public_url = public_url.rstrip("/") if public_url else None
        self.now = now

    def action_url(self, action: str, value: str) -> str | None:
        if not self.public_url:
            return None
        token = actions.sign(self.secret, action, value, self.now())
        return f"{self.public_url}/api/notify/action/{token}"

    async def send(self, n: OutgoingNotification) -> None:
        await self.client.publish(ntfy_payload(n, self.tz, self.action_url))
