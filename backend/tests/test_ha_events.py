import asyncio
import json
from typing import Any

import websockets

from app.notify.base import OutgoingNotification
from app.notify.ha_events import listen_once, parse_action, ws_url
from app.notify.homeassistant import HAConfig, ha_payload
from tests.conftest import Harness


def test_parse_action_accepts_only_our_actions() -> None:
    assert parse_action("ATRK_MUTE:ae1234") == ("mute", "ae1234")
    assert parse_action("ATRK_DOWN:12,13") == ("down", [12, 13])
    assert parse_action("ATRK_MUTE:../../etc") is None
    assert parse_action("SOMEONE_ELSE") is None
    assert parse_action("ATRK_DOWN:1;DROP") is None


def test_ws_url() -> None:
    assert ws_url(HAConfig("http://ha:8123/", "t", None)) == "ws://ha:8123/api/websocket"
    assert ws_url(HAConfig("https://ha.example", "t", None)) == "wss://ha.example/api/websocket"


def test_payload_has_action_buttons() -> None:
    n = OutgoingNotification("ae1234", "t", "m", "Aircraft", "high", alert_ids=(5, 6),
                             click_url="https://x.example/#/aircraft/ae1234")  # fmt: skip
    actions = ha_payload(n)["data"]["actions"]
    assert [a["title"] for a in actions] == ["Mute 1 day", "Not useful", "Open"]
    assert actions[1]["action"] == "ATRK_DOWN:5,6"
    assert actions[2]["uri"].endswith("ae1234")


async def test_listener_dispatches_events_from_home_assistant() -> None:
    got: list[tuple[str, Any]] = []
    done = asyncio.Event()

    async def fake_ha(ws: Any) -> None:
        await ws.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(await ws.recv())
        assert auth["access_token"] == "secret"
        await ws.send(json.dumps({"type": "auth_ok"}))
        sub = json.loads(await ws.recv())
        assert sub["event_type"] == "mobile_app_notification_action"
        await ws.send(json.dumps({"id": 1, "type": "result", "success": True}))
        for action in ("ATRK_MUTE:ae1234", "OTHER_APP_THING", "ATRK_DOWN:7"):
            await ws.send(json.dumps({"id": 1, "type": "event",
                                      "event": {"data": {"action": action}}}))  # fmt: skip
        await done.wait()

    async def handle(kind: str, value: Any) -> None:
        got.append((kind, value))
        if len(got) == 2:
            done.set()

    async with websockets.serve(fake_ha, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        cfg = HAConfig(f"http://127.0.0.1:{port}", "secret", None)
        await asyncio.wait_for(listen_once(cfg, handle), timeout=5)
    assert got == [("mute", "ae1234"), ("down", [7])]


async def test_runtime_handles_mute_and_thumbs_down(authed: Harness) -> None:
    rt = authed.ctx.rt()
    await rt.handle_notification_action("mute", "ae1234")
    ex = (await authed.client.get("/api/exclusions")).json()
    assert ex[0]["kind"] == "hex" and ex[0]["value"] == "ae1234" and ex[0]["until"]
    assert rt.exclusions and rt.exclusions[0].value == "ae1234"


async def test_photo_endpoint_uses_planespotters_with_credit(authed: Harness) -> None:
    import httpx
    import respx

    with respx.mock:
        route = respx.get("https://api.planespotters.net/pub/photos/hex/abc123").mock(
            return_value=httpx.Response(200, json={"photos": [{
                "thumbnail_large": {"src": "https://t.plnspttrs.net/1.jpg"},
                "link": "https://www.planespotters.net/photo/1", "photographer": "J. Doe"}]})
        )  # fmt: skip
        r = (await authed.client.get("/api/photos/abc123")).json()
        assert r["source"] == "planespotters"
        assert r["src"] == "https://t.plnspttrs.net/1.jpg"
        assert r["link"] == "https://www.planespotters.net/photo/1"
        assert r["credit"] == "J. Doe"
        assert "+https://" in route.calls[0].request.headers["user-agent"]
        await authed.client.get("/api/photos/abc123")
        assert route.call_count == 1  # cached
