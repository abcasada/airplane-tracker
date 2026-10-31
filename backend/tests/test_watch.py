"""Following specific aircraft anywhere: watchlist from filters, global lookups, notifications."""

import json
from typing import Any

import httpx
import respx

from app.filters.models import Condition, FilterDefinition, FilterOut, Group
from app.ingest.poller import WATCH_CIRCLE
from app.watch import REG_LOOKUP_INTERVAL_S, WATCH_INTERVAL_S, targets
from tests.conftest import Harness

NTFY = "http://ntfy"
FI_HEX = "https://opendata.adsb.fi/api/v2/hex/"
LOL_HEX = "https://api.adsb.lol/v2/hex/"
FI_REG = "https://opendata.adsb.fi/api/v2/registration/"
HOME = {"name": "Home", "lat": 38.84, "lon": -97.61, "elevation_ft": 1220, "radius_nm": 10,
        "is_home": True}  # fmt: skip


def flt(fid: int, root: Group, enabled: bool = True) -> FilterOut:
    return FilterOut(
        id=fid, name=f"f{fid}", description="", enabled=enabled, mode="live", sort_order=0,
        version=1, version_id=fid, definition=FilterDefinition(root=root), created_at=0,
        updated_at=0,
    )  # fmt: skip


def c(field: str, op: str, value: Any, enabled: bool = True) -> Condition:
    return Condition(field=field, op=op, value=value, enabled=enabled)


def test_targets_only_positive_enabled_conditions() -> None:
    filters = [
        flt(1, Group(children=[c("registration", "in", ["n628ts", " 05-5140 "]),
                               c("altitude_ft", "gt", 1000)])),
        flt(2, Group(mode="any", children=[c("hex", "eq", "AE1234"),
                                           c("registration", "eq", "N628TS")])),
        flt(3, Group(children=[c("registration", "not_in", ["N1"]),
                               c("registration", "in", ["N2"], enabled=False),
                               Group(mode="none", children=[c("hex", "eq", "abcdef")])])),
        flt(4, Group(children=[c("registration", "eq", "N4")]), enabled=False),
    ]  # fmt: skip
    assert targets(filters) == {
        ("reg", "N628TS"): {1, 2},
        ("reg", "05-5140"): {1},
        ("hex", "ae1234"): {2},
    }


async def _setup(h: Harness, regs: list[str]) -> int:
    db = h.ctx.db
    await db.executemany(
        "INSERT INTO aircraft_db(icao, reg, type_code, descr) VALUES (?, ?, ?, ?)",
        [("a835af", "N628TS", "GLF6", "GULFSTREAM G650"), ("ae0414", "05-5140", "C17", None)],
    )
    await db.executemany(
        "INSERT INTO airports(ident, icao, name, type, lat, lon, municipality) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [("KDOV", "KDOV", "Dover Air Force Base", "large_airport", 39.1295, -75.466, "Dover"),
         ("KSLN", "KSLN", "Salina Regional Airport", "medium_airport", 38.791, -97.652,
          "Salina")],
    )  # fmt: skip
    rt = h.ctx.rt()
    await rt.airport_index.load(db)
    c_ = h.client
    await c_.post("/api/places", json=HOME)
    await c_.put("/api/settings/ntfy", json={"url": NTFY, "topic": "planes", "token": "tk_test"})
    body = {
        "name": "Followed aircraft",
        "definition": {"root": {"mode": "all", "children": [
            {"type": "cond", "id": "a", "field": "registration", "op": "in", "value": regs},
        ]}},
    }  # fmt: skip
    r = await c_.post("/api/filters", json=body)
    assert r.status_code == 201, r.text
    fid: int = r.json()["id"]
    return fid


def ac(hex_: str, reg: str, lat: float, lon: float, **kw: Any) -> dict[str, Any]:
    return {"hex": hex_, "r": reg, "t": "GLF6", "desc": "GULFSTREAM G650", "lat": lat,
            "lon": lon, "alt_baro": 31000, "gs": 450, "track": 250, "flight": "EJA628",
            "seen": 1, "seen_pos": 1, "type": "adsb_icao"} | kw  # fmt: skip


def payload(h: Harness, *aircraft: dict[str, Any]) -> dict[str, Any]:
    return {"ac": list(aircraft), "now": h.clock.now() * 1000, "total": len(aircraft)}


@respx.mock
async def test_followed_aircraft_is_found_anywhere_and_notifies(authed: Harness) -> None:
    await _setup(authed, ["N628TS", "N999ZZ"])
    rt = authed.ctx.rt()
    assert rt.watch.hexes() == ["a835af"]  # N999ZZ isn't in the database
    hexes = respx.get(f"{FI_HEX}a835af").mock(
        side_effect=lambda _req: httpx.Response(
            200, json=payload(authed, ac("a835af", "N628TS", 39.2, -75.6))
        )
    )
    by_reg = respx.get(f"{FI_REG}N999ZZ").mock(
        return_value=httpx.Response(200, json=payload(authed))
    )
    sent = respx.post(f"{NTFY}/").mock(return_value=httpx.Response(200, json=[]))
    await rt.poller.watch_tick()
    assert hexes.call_count == 1 and sent.call_count == 1
    assert by_reg.call_count == 0  # the hex lookup used this second's budget
    authed.clock.advance(2)
    await rt.poller.watch_tick()
    assert by_reg.call_count == 1
    note = json.loads(sent.calls[0].request.content)
    assert note["title"] == "✈ N628TS GULFSTREAM G650 · 8 nm NW of Dover Air Force Base (KDOV)"
    assert "38.84" not in json.dumps(note) and "-97.61" not in json.dumps(note)
    assert note["message"].splitlines()[0] == "EJA628 · N628TS"
    assert "° up" not in note["message"]  # far below the horizon

    # Not due again for a minute; then one request covers the list again, and no repeat alert.
    await rt.poller.watch_tick()
    assert hexes.call_count == 1
    authed.clock.advance(WATCH_INTERVAL_S)
    await rt.poller.watch_tick()
    assert hexes.call_count == 2 and sent.call_count == 1
    assert by_reg.call_count == 1  # registration lookups are spaced out
    authed.clock.advance(REG_LOOKUP_INTERVAL_S)
    await rt.poller.watch_tick()
    authed.clock.advance(2)
    await rt.poller.watch_tick()
    assert by_reg.call_count == 2

    status = (await authed.client.get("/api/watch")).json()
    found = next(e for e in status if e["value"] == "N628TS")
    assert found["filters"] == ["Followed aircraft"] and found["hexes"] == ["a835af"]
    assert found["live"]["near"] == "8 nm NW of Dover Air Force Base (KDOV)"
    missing = next(e for e in status if e["value"] == "N999ZZ")
    assert missing["resolved"] is False and missing["live"] is None


@respx.mock
async def test_unknown_registration_resolves_by_lookup_and_fails_over(authed: Harness) -> None:
    await _setup(authed, ["N777QQ"])
    rt = authed.ctx.rt()
    respx.get(f"{FI_REG}N777QQ").mock(
        return_value=httpx.Response(200, json=payload(authed, ac("a0b1c2", "N777QQ", 38.8, -97.6)))
    )
    respx.post(f"{NTFY}/").mock(return_value=httpx.Response(200, json=[]))
    await rt.poller.watch_tick()  # nothing to look up by hex yet, so the lookup goes now
    assert rt.watch.hexes() == ["a0b1c2"]  # learned from the lookup
    # adsb.fi starts refusing: the breaker opens and adsb.lol answers the hex lookup.
    respx.get(f"{FI_HEX}a0b1c2").mock(return_value=httpx.Response(429))
    lol = respx.get(f"{LOL_HEX}a0b1c2").mock(
        return_value=httpx.Response(200, json=payload(authed, ac("a0b1c2", "N777QQ", 38.9, -97.6)))
    )
    authed.clock.advance(WATCH_INTERVAL_S)
    await rt.poller.watch_tick()
    assert lol.call_count == 1
    fi = next(s for s in rt.poller.status() if s["name"] == "adsb.fi")
    assert fi["healthy"] is False


async def test_registration_autocomplete_and_nearest_airport_fields(authed: Harness) -> None:
    await _setup(authed, ["N628TS"])
    r = await authed.client.get(
        "/api/fields/suggest", params={"source": "registration", "q": "n62"}
    )
    assert r.json()[0] == {"value": "N628TS", "label": "GLF6 · a835af"}
    r = await authed.client.get("/api/fields/suggest", params={"source": "airports", "q": "dover"})
    assert r.json()[0]["value"] == "KDOV"
    fields = {f["key"]: f for f in (await authed.client.get("/api/fields")).json()["fields"]}
    assert fields["registration"]["suggest"] == "registration"
    assert {"nearest_airport", "nearest_airport_nm", "near_description"} <= fields.keys()


def test_watch_circle_is_not_a_planner_circle() -> None:
    assert WATCH_CIRCLE.key == "watch" and WATCH_CIRCLE.tier == "watch"
