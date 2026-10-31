import json

import httpx
import respx

from tests.conftest import Harness

HOME = {"name": "Home", "lat": 38.84, "lon": -97.61, "radius_nm": 10, "is_home": True}


async def test_place_crud_and_single_home(authed: Harness) -> None:
    c = authed.client
    r = await c.post("/api/places", json=HOME)
    assert r.status_code == 201, r.text
    home = r.json()
    assert home["visibility"]["min_elevation_deg"] == 15
    r = await c.post("/api/places", json={**HOME, "name": "Cabin"})
    cabin = r.json()
    places = (await c.get("/api/places")).json()
    assert [p["name"] for p in places if p["is_home"]] == ["Cabin"]

    r = await c.put(f"/api/places/{home['id']}", json={**HOME, "radius_nm": 3})
    assert r.json()["radius_nm"] == 3
    assert (await c.delete(f"/api/places/{cabin['id']}")).status_code == 200
    assert (await c.delete(f"/api/places/{cabin['id']}")).status_code == 404


async def test_area_polygon_validation(authed: Harness) -> None:
    body = {"name": "Range", "role": "area", "lat": 38.7, "lon": -97.8, "radius_nm": 10,
            "polygon": [[38.7, -97.8], [38.6, -97.8]]}  # fmt: skip
    r = await authed.client.post("/api/places", json=body)
    assert r.status_code == 422


async def test_ntfy_settings_hide_token_and_validate(authed: Harness) -> None:
    c = authed.client
    body = {"url": "http://ntfy", "topic": "planes", "token": "tk_secret"}
    assert (await c.put("/api/settings/ntfy", json=body)).status_code == 200
    got = (await c.get("/api/settings/ntfy")).json()
    assert got["token_set"] is True and got["topic"] == "planes"
    assert "tk_secret" not in str(got)
    # Saving without a token keeps it; clear_token removes it.
    await c.put("/api/settings/ntfy", json={"url": "http://ntfy", "topic": "planes"})
    assert (await c.get("/api/settings/ntfy")).json()["token_set"] is True
    await c.put("/api/settings/ntfy", json={"url": "http://ntfy", "topic": "planes",
                                            "clear_token": True})  # fmt: skip
    assert (await c.get("/api/settings/ntfy")).json()["token_set"] is False
    bad = {"url": "ntfy", "topic": "planes"}
    assert (await c.put("/api/settings/ntfy", json=bad)).status_code == 422
    bad = {"url": "http://ntfy", "topic": "my topic/../x"}
    assert (await c.put("/api/settings/ntfy", json=bad)).status_code == 422


@respx.mock
async def test_ntfy_connection_test_sends_a_notification(authed: Harness) -> None:
    c = authed.client
    assert (await c.post("/api/settings/ntfy/test")).status_code == 400  # not configured
    await c.put("/api/settings/ntfy", json={"url": "http://ntfy", "topic": "planes",
                                            "token": "tk_x"})  # fmt: skip
    respx.get("http://ntfy/v1/health").mock(
        return_value=httpx.Response(200, json={"healthy": True})
    )
    sent = respx.post("http://ntfy/").mock(return_value=httpx.Response(200, json={"id": "1"}))
    r = await c.post("/api/settings/ntfy/test")
    assert r.json() == {"ok": True}
    body = json.loads(sent.calls[0].request.content)
    assert body["topic"] == "planes" and "Test notification" in body["message"]
    assert sent.calls[0].request.headers["authorization"] == "Bearer tk_x"


@respx.mock
async def test_ntfy_rejected_token_is_reported(authed: Harness) -> None:
    c = authed.client
    await c.put("/api/settings/ntfy", json={"url": "http://ntfy", "topic": "planes",
                                            "token": "tk_bad"})  # fmt: skip
    respx.get("http://ntfy/v1/health").mock(
        return_value=httpx.Response(200, json={"healthy": True})
    )
    respx.post("http://ntfy/").mock(return_value=httpx.Response(403))
    r = await c.post("/api/settings/ntfy/test")
    assert r.json()["ok"] is False and "403" in r.json()["error"]
    respx.get("http://ntfy/v1/health").mock(side_effect=httpx.ConnectError("refused"))
    r = await c.post("/api/settings/ntfy/test")
    assert "Can't reach ntfy" in r.json()["error"]


async def test_prediction_settings_and_place_override(authed: Harness) -> None:
    c = authed.client
    s = (await c.get("/api/settings/prediction")).json()
    assert s["enabled"] is True and s["lead_max_s"] == 600 and s["min_confidence"] == 0.6
    s["min_confidence"] = 0.75
    r = await c.put("/api/settings/prediction", json=s)
    assert r.status_code == 200 and r.json()["min_confidence"] == 0.75
    assert authed.ctx.rt().predictor.settings.min_confidence == 0.75
    assert (
        await c.put("/api/settings/prediction", json={**s, "min_confidence": 2})
    ).status_code == 422

    home = {"name": "Home", "lat": 38.84, "lon": -97.61, "radius_nm": 10,
            "prediction": {"lead_max_s": 300, "min_confidence": None}}  # fmt: skip
    p = (await c.post("/api/places", json=home)).json()
    assert p["prediction"] == {"lead_max_s": 300, "min_confidence": None}
    got = next(x for x in (await c.get("/api/places")).json() if x["id"] == p["id"])
    assert got["prediction"]["lead_max_s"] == 300
