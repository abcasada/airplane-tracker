"""FAA special use airspace search (synthetic responses; tests never hit the real service)."""

from typing import Any

import httpx
import pytest
import respx

from app.sources.airspace import SUA_URL, altitude, parse_schedule, time_zone
from tests.conftest import Harness

SQUARE = [[[-97.9, 38.6], [-97.8, 38.6], [-97.8, 38.7], [-97.9, 38.7], [-97.9, 38.6]]]


def feature(name: str, rings: list[Any], **attrs: Any) -> dict[str, Any]:
    base = {
        "NAME": name, "TYPE_CODE": "R", "LOWER_VAL": "0", "LOWER_UOM": "FT", "LOWER_CODE": "SFC",
        "UPPER_VAL": "180", "UPPER_UOM": "FL", "UPPER_CODE": "STD", "CITY": "SOMEWHERE",
        "STATE": "KS", "TIMESOFUSE": "0900 - 1700, MON - SAT; O/T BY NOTAM",
        "CONT_AGENT": "FAA, EXAMPLE ARTCC", "GMTOFFSET": "-6", "DST_CODE": "1",
    }  # fmt: skip
    return {"attributes": base | attrs, "geometry": {"rings": rings}}


RESPONSE = {
    "features": [
        feature("R-9999A", SQUARE),
        feature("R-9999B", SQUARE, LOWER_VAL="180", LOWER_UOM="FL", LOWER_CODE="STD",
                UPPER_VAL="230"),
        feature("EXAMPLE MOA", [SQUARE[0], [[-97.0, 38.0], [-96.99, 38.0], [-96.99, 38.01]]],
                TYPE_CODE="MOA", LOWER_VAL="500", LOWER_UOM="FT", LOWER_CODE="SFC",
                UPPER_VAL="-9998", UPPER_UOM=None, UPPER_CODE="UNLTD",
                TIMESOFUSE="INTERMITTENT BY NOTAM"),
    ]
}  # fmt: skip


def test_time_zones() -> None:
    assert time_zone("-6", "1") == "America/Chicago"
    assert time_zone("-7", "0") == "America/Phoenix"
    assert time_zone("3", "0") is None and time_zone(None, None) is None


def test_altitudes() -> None:
    assert altitude("180", "FL", "STD", False).ft == 18000
    assert altitude("0", "FT", "SFC", True).ft is None
    floor = altitude("500", "FT", "SFC", True)
    assert floor.ft == 500 and floor.ref == "AGL"
    assert altitude("-9998", None, "UNLTD", False).ref == "UNL"
    assert altitude("12000", "FT", "MSL", False).text == "12,000 ft MSL"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0900 - 1700, MON - SAT; O/T BY NOTAM 6 HOURS IN ADVANCE",
         {"days": [0, 1, 2, 3, 4, 5], "start": "09:00", "end": "17:00"}),
        ("0700 - 2200, DAILY", {"days": [0, 1, 2, 3, 4, 5, 6], "start": "07:00", "end": "22:00"}),
        ("1400 - 0400, FRI - MON", {"days": [0, 4, 5, 6], "start": "14:00", "end": "04:00"}),
        ("0700 - 1800, MON - THU; 0700 - 1600, FRI", None),
        ("CONTINUOUS", None),
        (None, None),
    ],
)  # fmt: skip
def test_schedule_parsing(text: str | None, expected: dict[str, Any] | None) -> None:
    assert parse_schedule(text) == expected


@respx.mock
async def test_search_merges_stacked_sectors_and_caches(authed: Harness) -> None:
    route = respx.get(SUA_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    r = await authed.client.get("/api/airspace/search", params={"q": "r-9999'; drop"})
    assert r.status_code == 200, r.text
    assert route.calls[0].request.url.params["where"] == "NAME LIKE '%R-9999 DROP%'"
    body = r.json()
    assert "FAA" in body["attribution"]
    by_name = {a["name"]: a for a in body["results"]}
    assert set(by_name) == {"R-9999A/B", "EXAMPLE MOA"}
    r9 = by_name["R-9999A/B"]
    assert r9["floor"]["ref"] == "SFC" and r9["ceiling"]["ft"] == 23000
    assert r9["polygon"][0] == [38.6, -97.9] and len(r9["polygon"]) == 4  # closing point dropped
    assert r9["schedule"] == {
        "days": [0, 1, 2, 3, 4, 5], "start": "09:00", "end": "17:00", "tz": "America/Chicago",
    }  # fmt: skip
    assert r9["type"] == "Restricted" and r9["location"] == "Somewhere, KS"
    moa = by_name["EXAMPLE MOA"]
    assert moa["floor"] == {"ft": 500, "ref": "AGL", "text": "500 ft AGL"}
    assert moa["ceiling"]["ref"] == "UNL" and moa["schedule"] is None
    assert moa["parts_dropped"] == 1
    # Cached: a repeat search doesn't call the FAA again.
    await authed.client.get("/api/airspace/search", params={"q": "R-9999"})
    await authed.client.get("/api/airspace/search", params={"q": "r-9999 drop"})
    assert route.call_count == 2


@respx.mock
async def test_search_near_a_point_and_errors(authed: Harness) -> None:
    route = respx.get(SUA_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    r = await authed.client.get(
        "/api/airspace/search", params={"lat": 38.84, "lon": -97.61, "radius_nm": 30}
    )
    assert r.status_code == 200
    params = route.calls[0].request.url.params
    assert params["geometryType"] == "esriGeometryEnvelope" and params["inSR"] == "4326"
    assert (await authed.client.get("/api/airspace/search")).status_code == 422
    respx.get(SUA_URL).mock(return_value=httpx.Response(500, json={"error": {"code": 500}}))
    r = await authed.client.get("/api/airspace/search", params={"q": "OTHER"})
    assert r.status_code == 502
