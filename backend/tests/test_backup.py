"""Backup export and merge-restore."""

import json
from typing import Any

from tests.conftest import Harness
from tests.test_replay import HOME, RANGE, cond


async def _configure(h: Harness) -> dict[str, int]:
    c = h.client
    home = (await c.post("/api/places", json=HOME)).json()["id"]
    rng = (await c.post("/api/places", json=RANGE)).json()["id"]
    body = {
        "name": "Range traffic", "mode": "shadow",
        "definition": {
            "display_place_id": home,
            "root": {"mode": "all", "children": [
                cond("inside_area", "is_true", rng), cond("visible_now", "is_true", 0),
            ]},
        },
    }  # fmt: skip
    fid = (await c.post("/api/filters", json=body)).json()["id"]
    await c.post("/api/exclusions", json={"filter_id": fid, "kind": "hex", "value": "ae0001"})
    await c.post("/api/exclusions", json={"kind": "callsign", "value": "N123AB"})
    await c.post("/api/military/overrides", json={"key": "hex:a12345", "is_military": True})
    s = (await c.get("/api/settings/prediction")).json()
    await c.put("/api/settings/prediction", json=s | {"min_confidence": 0.7})
    await c.put("/api/settings/ha", json={"url": "http://ha:8123", "token": "SECRET-TOKEN",
                                          "service": "mobile_app_x"})  # fmt: skip
    return {"home": home, "range": rng, "filter": fid}


async def test_export_has_configuration_but_no_credentials(authed: Harness) -> None:
    await _configure(authed)
    r = await authed.client.get("/api/backup")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    text = r.text
    assert "SECRET-TOKEN" not in text and "http://ha:8123" not in text
    assert "password" not in text and "totp" not in text.lower()
    data = r.json()
    assert data["format"] == "airplane-tracker-backup/1"
    assert {p["name"] for p in data["places"]} == {"Home", "Range"}
    assert {f["name"] for f in data["filters"]} >= {"Range traffic", "Emergency squawks"}
    assert len(data["exclusions"]) == 2
    assert data["settings"]["prediction"]["min_confidence"] == 0.7
    assert data["military_overrides"][0]["key"] == "hex:a12345"


async def test_restore_remaps_ids_and_is_idempotent(authed: Harness) -> None:
    c = authed.client
    ids = await _configure(authed)
    data: dict[str, Any] = (await c.get("/api/backup")).json()
    # Wipe: delete the filter and places, mutes and override, reset a setting.
    await c.delete(f"/api/filters/{ids['filter']}")
    for x in (await c.get("/api/exclusions")).json():
        await c.delete(f"/api/exclusions/{x['id']}")
    await c.delete(f"/api/places/{ids['range']}")
    await c.delete(f"/api/places/{ids['home']}")
    await c.delete("/api/military/overrides/hex:a12345")
    s = (await c.get("/api/settings/prediction")).json()
    await c.put("/api/settings/prediction", json=s | {"min_confidence": 0.6})

    # Another place first, so restored places can't get their old ids back.
    spare = {"name": "Spare", "lat": 30.0, "lon": -90.0, "radius_nm": 5}
    assert (await c.post("/api/places", json=spare)).status_code == 201

    r = await c.post("/api/backup/restore", json=data)
    assert r.status_code == 200, r.text
    counts = r.json()
    assert counts["places"] == 2 and counts["filters"] == 1 and counts["filters_existing"] == 1
    assert counts["exclusions"] == 2 and counts["overrides"] == 1 and counts["settings"] == 3
    places = {p["name"]: p for p in (await c.get("/api/places")).json()}
    assert places["Home"]["is_home"] is True
    new_range = places["Range"]["id"]
    assert new_range != ids["range"]
    flt = next(f for f in (await c.get("/api/filters")).json() if f["name"] == "Range traffic")
    children = flt["definition"]["root"]["children"]
    assert children[0]["place"] == new_range and children[1]["place"] == 0
    assert flt["definition"]["display_place_id"] == places["Home"]["id"]
    assert flt["errors"] == [] and flt["mode"] == "shadow"
    mutes = (await c.get("/api/exclusions")).json()
    assert {m["filter_id"] for m in mutes} == {flt["id"], None}
    assert (await c.get("/api/settings/prediction")).json()["min_confidence"] == 0.7
    assert authed.ctx.rt().predictor.settings.min_confidence == 0.7

    again = (await c.post("/api/backup/restore", json=data)).json()
    assert again["places"] == 0 and again["places_existing"] == 2
    assert again["filters"] == 0 and again["exclusions"] == 0
    assert len((await c.get("/api/places")).json()) == 3


async def test_restore_keeps_this_installs_home_and_validates(authed: Harness) -> None:
    c = authed.client
    await _configure(authed)
    data = (await c.get("/api/backup")).json()
    data["places"][0] |= {"name": "Old home", "lat": 40.0}
    await c.post("/api/backup/restore", json=data)
    places = {p["name"]: p for p in (await c.get("/api/places")).json()}
    assert places["Home"]["is_home"] is True and places["Old home"]["is_home"] is False
    bad = json.loads(json.dumps(data)) | {"format": "something-else"}
    assert (await c.post("/api/backup/restore", json=bad)).status_code == 422
    bad = data | {"military_overrides": [{"key": "nope", "is_military": True}]}
    assert (await c.post("/api/backup/restore", json=bad)).status_code == 422
