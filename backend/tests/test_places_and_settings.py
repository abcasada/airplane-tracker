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


async def test_ha_settings_hide_token(authed: Harness) -> None:
    c = authed.client
    r = await c.put("/api/settings/ha", json={"url": "http://ha:8123", "token": "secret-token"})
    assert r.status_code == 200
    got = (await c.get("/api/settings/ha")).json()
    assert got["token_set"] is True
    assert "secret-token" not in str(got)


@respx.mock
async def test_ha_connection_test_lists_mobile_services(authed: Harness) -> None:
    c = authed.client
    await c.put("/api/settings/ha", json={"url": "http://ha:8123", "token": "t"})
    respx.get("http://ha:8123/api/").mock(
        return_value=httpx.Response(200, json={"message": "API running."})
    )
    respx.get("http://ha:8123/api/services").mock(
        return_value=httpx.Response(
            200,
            json=[{"domain": "notify", "services": {"mobile_app_pixel": {}, "persistent": {}}}],
        )
    )
    r = await c.post("/api/settings/ha/test")
    assert r.json() == {"ok": True, "message": "API running.", "services": ["mobile_app_pixel"]}


@respx.mock
async def test_ha_bad_token_is_reported(authed: Harness) -> None:
    c = authed.client
    await c.put("/api/settings/ha", json={"url": "http://ha:8123", "token": "bad"})
    respx.get("http://ha:8123/api/").mock(return_value=httpx.Response(401))
    r = await c.post("/api/settings/ha/test")
    assert r.json()["ok"] is False
    assert "401" in r.json()["error"]
