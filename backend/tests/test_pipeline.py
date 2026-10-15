"""End to end: aircraft reports → filters → alert episodes → Home Assistant notifications."""

import json
from typing import Any

import httpx
import pytest
import respx

from app.sources.base import Observation, SourceBatch
from tests.conftest import Harness

HA = "http://ha:8123"
HOME = {"name": "Home", "lat": 38.84, "lon": -97.61, "elevation_ft": 1220, "radius_nm": 10,
        "is_home": True}  # fmt: skip
MIL_VISIBLE = {
    "name": "Military over home",
    "definition": {
        "display_place_id": None,
        "root": {"mode": "all", "children": [
            {"type": "cond", "id": "a", "field": "military", "op": "is_true"},
            {"type": "cond", "id": "b", "field": "visible_now", "op": "is_true", "place": None},
        ]},
    },
}  # fmt: skip


def c17(clock_now: float, **kw: Any) -> Observation:
    base: dict[str, Any] = dict(
        hex="ae1234", source="adsb.fi", seen_at=clock_now, pos_time=clock_now, lat=38.86,
        lon=-97.60, alt_baro_ft=4220, gs_kt=250, track_deg=180, callsign="RCH452",
        squawk="1234", category="A5", type_code="C17", registration="05-5140", db_flags=1,
        type_desc="BOEING C-17 Globemaster 3",
    )  # fmt: skip
    base.update(kw)
    return Observation(**base)


async def _setup(h: Harness) -> int:
    c = h.client
    home = (await c.post("/api/places", json=HOME)).json()
    await c.put("/api/settings/ha", json={"url": HA, "token": "t", "service": "mobile_app_x"})
    body = json.loads(json.dumps(MIL_VISIBLE))
    body["definition"]["display_place_id"] = home["id"]
    body["definition"]["root"]["children"][1]["place"] = home["id"]
    r = await c.post("/api/filters", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["errors"] == []
    fid: int = r.json()["id"]
    return fid


async def _feed(h: Harness, *obs: Observation) -> None:
    rt = h.ctx.rt()
    await rt.on_batch(SourceBatch("adsb.fi", h.clock.now(), list(obs)), rt.circles()[0])


@respx.mock
async def test_match_notifies_once_per_episode(authed: Harness) -> None:
    fid = await _setup(authed)
    sent = respx.post(f"{HA}/api/services/notify/mobile_app_x").mock(
        return_value=httpx.Response(200, json=[])
    )
    await _feed(authed, c17(authed.clock.now()))
    assert sent.call_count == 1
    payload = json.loads(sent.calls[0].request.content)
    assert payload["title"].startswith("✈ BOEING C-17 Globemaster 3 · 1.")
    assert payload["title"].endswith("nm NNE of Home")
    assert "RCH452" in payload["message"] and "Military:" in payload["message"]
    assert payload["data"]["tag"] == "aircraft-ae1234"
    assert payload["data"]["channel"] == "Aircraft"

    # Still matching: same episode, no new notification.
    authed.clock.advance(10)
    await _feed(authed, c17(authed.clock.now(), lat=38.85))
    assert sent.call_count == 1

    alerts = (await authed.client.get("/api/alerts")).json()
    assert len(alerts) == 1
    assert alerts[0]["notified"] == 1 and alerts[0]["state"] == "active"
    detail = (await authed.client.get(f"/api/alerts/{alerts[0]['id']}")).json()
    assert detail["explanation"]["result"] is True
    assert detail["notifications"][0]["ok"] == 1

    # Leaves view; after the episode timeout it ends, and a later match is a new episode.
    authed.clock.advance(10)
    await _feed(authed, c17(authed.clock.now(), lat=39.3))
    authed.clock.advance(130)
    await _feed(authed, c17(authed.clock.now(), lat=39.3))
    alerts = (await authed.client.get("/api/alerts")).json()
    assert alerts[0]["state"] == "ended"
    authed.clock.advance(10)
    await _feed(authed, c17(authed.clock.now()))
    assert sent.call_count == 2
    flt = (await authed.client.get(f"/api/filters/{fid}")).json()
    assert flt["alerts_today"] == 2


@respx.mock
async def test_optional_repeat_suppression_and_shadow_mode(authed: Harness) -> None:
    fid = await _setup(authed)
    sent = respx.post(f"{HA}/api/services/notify/mobile_app_x").mock(
        return_value=httpx.Response(200, json=[])
    )
    f = (await authed.client.get(f"/api/filters/{fid}")).json()
    f["definition"]["notify"]["repeat_suppress_min"] = 60
    body = {k: f[k] for k in ("name", "description", "enabled", "mode", "definition")}
    assert (await authed.client.put(f"/api/filters/{fid}", json=body)).status_code == 200
    await _feed(authed, c17(authed.clock.now()))
    authed.clock.advance(200)
    await _feed(authed, c17(authed.clock.now(), lat=39.3))  # ends the episode
    authed.clock.advance(10)
    await _feed(authed, c17(authed.clock.now()))
    assert sent.call_count == 1
    alerts = (await authed.client.get("/api/alerts")).json()
    assert "last 60 min" in alerts[0]["suppressed_reason"]

    await authed.client.patch(f"/api/filters/{fid}", json={"mode": "shadow"})
    await _feed(authed, c17(authed.clock.now(), hex="ae9999", callsign="RCH9"))
    assert sent.call_count == 1
    shadow = (await authed.client.get("/api/alerts?mode=shadow")).json()
    assert shadow[0]["suppressed_reason"] == "shadow mode"


@respx.mock
async def test_combined_notification_and_exclusions(authed: Harness) -> None:
    fid = await _setup(authed)
    second = {"name": "C-17s", "definition": {"root": {"children": [
        {"type": "cond", "field": "type_code", "op": "in", "value": ["C17"]}]}}}  # fmt: skip
    assert (await authed.client.post("/api/filters", json=second)).status_code == 201
    sent = respx.post(f"{HA}/api/services/notify/mobile_app_x").mock(
        return_value=httpx.Response(200, json=[])
    )
    await _feed(authed, c17(authed.clock.now()))
    assert sent.call_count == 1
    msg = json.loads(sent.calls[0].request.content)["message"]
    assert "Military over home" in msg and "C-17s" in msg

    r = await authed.client.post(
        "/api/exclusions", json={"filter_id": None, "kind": "type_code", "value": "c17"}
    )
    assert r.status_code == 201
    await _feed(authed, c17(authed.clock.now(), hex="ae7777", callsign="RCH7"))
    assert sent.call_count == 1
    assert fid


@respx.mock
async def test_ha_failure_is_recorded(authed: Harness) -> None:
    await _setup(authed)
    respx.post(f"{HA}/api/services/notify/mobile_app_x").mock(return_value=httpx.Response(500))
    await _feed(authed, c17(authed.clock.now()))
    alert = (await authed.client.get("/api/alerts")).json()[0]
    assert alert["notified"] == 0
    assert alert["suppressed_reason"].startswith("send failed")


async def test_preview_funnel_and_explain(authed: Harness) -> None:
    await _setup(authed)
    rt = authed.ctx.rt()
    await rt.ingest(SourceBatch("adsb.fi", authed.clock.now(), [
        c17(authed.clock.now()),
        c17(authed.clock.now(), hex="a00001", callsign="UAL1", type_code="B738", db_flags=0,
            registration="N1", category="A3", type_desc="BOEING 737-800"),
    ]))  # fmt: skip
    home_id = rt.home().id  # type: ignore[union-attr]
    definition = {"display_place_id": home_id, "root": {"mode": "all", "children": [
        {"type": "cond", "id": "a", "field": "type_code", "op": "in", "value": ["C17", "B738"]},
        {"type": "cond", "id": "b", "field": "military", "op": "is_true"},
    ]}}  # fmt: skip
    r = (await authed.client.post("/api/filters/preview", json={"definition": definition})).json()
    assert r["match_count"] == 1 and r["matches"][0]["hex"] == "ae1234"
    assert [s["count"] for s in r["funnel"]] == [2, 2, 1]
    assert r["summary"] == "Aircraft type is C17, B738 AND Military"
    ex = (
        await authed.client.post(
            "/api/filters/explain", json={"definition": definition, "hex": "a00001"}
        )
    ).json()
    assert ex["matches"] is False
    assert [c["result"] for c in ex["explanation"]["children"]] == [True, False]

    bad = {"root": {"children": [{"type": "cond", "field": "visible_now", "op": "is_true"}]}}
    r = (await authed.client.post("/api/filters/preview", json={"definition": bad})).json()
    assert r["errors"] and r["match_count"] == 0

    detail = (await authed.client.get("/api/aircraft/ae1234")).json()
    assert detail["facts"]["military"] is True
    assert detail["places"][0]["facts"]["visible_now"] is True


async def test_filter_versions_revert_and_starter_filter(authed: Harness) -> None:
    c = authed.client
    filters = (await c.get("/api/filters")).json()
    assert [f["name"] for f in filters] == ["Emergency squawks"]
    fid = filters[0]["id"]
    body = {k: filters[0][k] for k in ("name", "description", "enabled", "mode", "definition")}
    body["name"] = "Emergencies"
    assert (await c.put(f"/api/filters/{fid}", json=body)).json()["version"] == 2
    versions = (await c.get(f"/api/filters/{fid}/versions")).json()
    assert [v["version"] for v in versions] == [2, 1]
    r = await c.post(f"/api/filters/{fid}/revert", json={"version_id": versions[1]["id"]})
    assert r.json()["name"] == "Emergency squawks" and r.json()["version"] == 3
    exported = (await c.get("/api/filters/export")).json()
    r = await c.post("/api/filters/import", json={"filters": exported["filters"]})
    assert r.json() == {"imported": 1}
    modes = sorted(f["mode"] for f in (await c.get("/api/filters")).json())
    assert modes == ["live", "shadow"]


@pytest.mark.parametrize("source", ["type_code", "plane_alert_category", "callsign", "nope"])
async def test_suggest_endpoints(authed: Harness, source: str) -> None:
    r = await authed.client.get(f"/api/fields/suggest?source={source}&q=c")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


async def test_status_and_fields(authed: Harness) -> None:
    status = (await authed.client.get("/api/status")).json()
    assert status["tracks"] == 0 and len(status["sources"]) == 2
    fields = (await authed.client.get("/api/fields")).json()
    assert any(f["key"] == "visible_now" for f in fields["fields"])
