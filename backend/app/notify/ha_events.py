"""Receives taps on notification action buttons through Home Assistant's WebSocket API.

The companion app fires `mobile_app_notification_action` in Home Assistant when a button is
tapped; we subscribe to that event and act on our own (prefixed) actions only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from .homeassistant import ACTION_PREFIX, HAConfig

log = logging.getLogger(__name__)

MUTE_SECONDS = 86400.0
_MUTE = re.compile(rf"^{ACTION_PREFIX}MUTE:(~?[0-9a-f]{{6}})$")
_DOWN = re.compile(rf"^{ACTION_PREFIX}DOWN:(\d+(?:,\d+){{0,20}})$")


def parse_action(action: str) -> tuple[str, Any] | None:
    """('mute', hex) or ('down', [alert ids]), or None for anything that isn't ours."""
    if m := _MUTE.match(action):
        return "mute", m.group(1)
    if m := _DOWN.match(action):
        return "down", [int(x) for x in m.group(1).split(",")]
    return None


def ws_url(cfg: HAConfig) -> str:
    base = cfg.base
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/api/websocket"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/api/websocket"
    return base + "/api/websocket"


Handler = Callable[[str, Any], Awaitable[None]]


async def listen_once(
    cfg: HAConfig, handle: Handler, on_ready: Callable[[], None] | None = None
) -> None:
    """One connection: authenticate, subscribe, dispatch until the socket closes."""
    async with websockets.connect(ws_url(cfg), open_timeout=10, max_size=2**20) as ws:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"unexpected HA hello: {hello.get('type')}")
        await ws.send(json.dumps({"type": "auth", "access_token": cfg.token}))
        auth = json.loads(await ws.recv())
        if auth.get("type") != "auth_ok":
            raise PermissionError("Home Assistant rejected the access token")
        await ws.send(
            json.dumps(
                {
                    "id": 1,
                    "type": "subscribe_events",
                    "event_type": "mobile_app_notification_action",
                }
            )
        )
        if on_ready:
            on_ready()
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") != "event":
                continue
            action = str((msg.get("event") or {}).get("data", {}).get("action", ""))
            parsed = parse_action(action)
            if parsed:
                try:
                    await handle(*parsed)
                except Exception:
                    log.exception("notification action %s failed", action)


class HAEventListener:
    def __init__(
        self, get_config: Callable[[], Awaitable[HAConfig | None]], handle: Handler
    ) -> None:
        self.get_config = get_config
        self.handle = handle
        self.connected = False
        self.last_error: str | None = None

    def _ready(self) -> None:
        self.connected = True
        self.last_error = None

    async def run(self) -> None:
        delay = 5.0
        while True:
            cfg = await self.get_config()
            if cfg is None:
                await asyncio.sleep(60)
                continue
            try:
                await listen_once(cfg, self.handle, self._ready)
                delay = 5.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = str(e) or type(e).__name__
                log.info("HA event connection ended: %s", self.last_error)
            self.connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300.0)
