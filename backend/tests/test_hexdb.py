"""hexdb.io lookups: who gets looked up, pacing, caching and backoff. All requests are mocked."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx

from app.clock import FakeClock
from app.db import Database
from app.enrich import hexdb
from app.enrich.enricher import Enricher
from app.sources.base import Observation, SourceBatch
from tests.conftest import Harness

HIT = {
    "hex": "4CA87C", "registration": "EI-FMV", "manufacturer": "Airbus", "type": "A320 214",
    "icao_type_code": "A320", "registered_owner": "Example Leasing", "operator_flag_code": "XYZ",
    "images": {"full": None, "thumbnail": None}, "links": {"self": "/api/v2/aircraft/4CA87C"},
}  # fmt: skip
NOT_FOUND = {
    "error": {"status": 404, "code": "aircraft_not_found", "message": "No aircraft with hex."}
}


def url(hex_: str) -> str:
    return hexdb.AIRCRAFT_URL.format(hex=hex_)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    d = Database(tmp_path / "db.sqlite3")
    await d.connect()
    yield d
    await d.close()


def test_parse_aircraft() -> None:
    assert hexdb.parse_aircraft(HIT) == {
        "registration": "EI-FMV", "type_code": "A320", "type_desc": "Airbus A320 214",
        "owner_op": "Example Leasing",
    }  # fmt: skip
    assert hexdb.parse_aircraft({"registration": "n/a", "type": "  "}) is None
    assert hexdb.parse_aircraft(["not", "a", "dict"]) is None


@respx.mock
async def test_hit_is_cached_and_fills_only_gaps(db: Database) -> None:
    respx.get(url("4ca87c")).mock(return_value=httpx.Response(200, json=HIT))
    clock = FakeClock()
    async with httpx.AsyncClient() as http:
        svc = hexdb.HexdbService(db, http, clock)
        svc.want("4ca87c")
        assert await svc.flush(lambda h: True) == "4ca87c"
        assert svc.status()["found"] == 1 and not svc.pending
    await db.execute("INSERT INTO aircraft_db(icao, reg) VALUES ('4ca87c', 'EI-OLD')")
    enr = Enricher(db, clock)
    info = await enr.lookup("4ca87c")
    # tar1090-db's registration wins; hexdb fills type and owner.
    assert info.registration == "EI-OLD" and info.type_code == "A320"
    assert info.owner_op == "Example Leasing" and info.type_desc == "Airbus A320 214"
    assert info.provenance == {"type_code": "hexdb", "owner_op": "hexdb", "type_desc": "hexdb"}
    assert info.hexdb_checked
    # After 30 days the answer is stale: still used, but worth asking again.
    clock.advance(31 * 86400)
    enr.forget("4ca87c")
    stale = await enr.lookup("4ca87c")
    assert stale.type_code == "A320" and not stale.hexdb_checked


@respx.mock
async def test_not_found_is_a_short_lived_miss(db: Database) -> None:
    route = respx.get(url("abcdef")).mock(return_value=httpx.Response(404, json=NOT_FOUND))
    clock = FakeClock()
    async with httpx.AsyncClient() as http:
        svc = hexdb.HexdbService(db, http, clock)
        svc.want("abcdef")
        assert await svc.flush(lambda h: True) == "abcdef"
        assert svc.status()["not_found"] == 1 and not svc.breaker.is_open(clock.now())
        enr = Enricher(db, clock)
        assert (await enr.lookup("abcdef")).hexdb_checked
        # Queued again within 3 days: the cached miss answers, no request.
        clock.advance(3600)
        svc.want("abcdef")
        assert await svc.flush(lambda h: True) is None and route.call_count == 1
        clock.advance(3 * 86400)
        enr.forget("abcdef")
        assert not (await enr.lookup("abcdef")).hexdb_checked


@respx.mock
async def test_pacing_priority_relevance_and_daily_cap(db: Database) -> None:
    respx.get(url__regex=r"https://hexdb\.io/api/v2/aircraft/.*").mock(
        return_value=httpx.Response(404, json=NOT_FOUND)
    )
    clock = FakeClock()
    async with httpx.AsyncClient() as http:
        svc = hexdb.HexdbService(db, http, clock, daily_cap=2)
        for h in ("aaaaa1", "aaaaa2", "aaaaa3"):
            svc.want(h)
        svc.want("~1234ab")  # non-ICAO addresses are never looked up
        svc.want("not-hex")
        svc.prioritize("aaaaa3")  # it matched a filter
        tracked = {"aaaaa1", "aaaaa3"}  # aaaaa2 left the area
        assert await svc.flush(tracked.__contains__) == "aaaaa3"
        assert "aaaaa2" not in svc.pending
        assert await svc.flush(tracked.__contains__) is None  # < 2 s since the last request
        clock.advance(hexdb.MIN_INTERVAL_S)
        assert await svc.flush(tracked.__contains__) == "aaaaa1"
        svc.want("aaaaa4")
        clock.advance(hexdb.MIN_INTERVAL_S)
        assert await svc.flush(lambda h: True) is None  # daily cap of 2 reached
        clock.advance(86400)
        assert await svc.flush(lambda h: True) == "aaaaa4"
        assert svc.requests == 3


@respx.mock
async def test_errors_back_off_hard(db: Database) -> None:
    route = respx.get(url("aaaaa1")).mock(
        return_value=httpx.Response(429, headers={"retry-after": "600"})
    )
    clock = FakeClock()
    async with httpx.AsyncClient() as http:
        svc = hexdb.HexdbService(db, http, clock)
        svc.want("aaaaa1")
        assert await svc.flush(lambda h: True) is None
        assert svc.status()["backing_off"] and "429" in str(svc.last_error)
        svc.want("aaaaa1")
        clock.advance(300)
        assert await svc.flush(lambda h: True) is None and route.call_count == 1
        clock.advance(301)
        # A 404 that isn't the documented "not found" answer (e.g. the API moved) is an error.
        route.mock(return_value=httpx.Response(404, text="nope"))
        assert await svc.flush(lambda h: True) is None and route.call_count == 2
        assert svc.breaker.is_open(clock.now())
        assert await db.fetchone("SELECT 1 FROM cache WHERE namespace = 'hexdb'") is None
        # 5xx backs off too.
        clock.advance(3600)
        svc.want("aaaaa1")
        route.mock(return_value=httpx.Response(503))
        assert await svc.flush(lambda h: True) is None
        assert svc.breaker.is_open(clock.now() + 29)


async def test_disabled_queues_nothing(db: Database) -> None:
    async with httpx.AsyncClient() as http:
        svc = hexdb.HexdbService(db, http, FakeClock(), enabled=False)
        svc.want("aaaaa1")
        assert not svc.pending


@respx.mock
async def test_runtime_queues_unknown_aircraft_and_reenriches(authed: Harness) -> None:
    rt = authed.ctx.rt()
    now = authed.clock.now()
    known = Observation(hex="a12345", source="adsb.fi", seen_at=now, lat=38.8, lon=-97.6,
                        registration="N123AB", type_code="C172", owner_op="DOE JOHN")  # fmt: skip
    unknown = Observation(hex="4ca87c", source="adsb.fi", seen_at=now, lat=38.8, lon=-97.6)
    await rt.ingest(SourceBatch(source="adsb.fi", fetched_at=now, observations=[known, unknown]))
    assert list(rt.hexdb.pending) == ["4ca87c"]
    route = respx.get(url("4ca87c")).mock(return_value=httpx.Response(200, json=HIT))
    await rt.hexdb_step()
    track = rt.store.get("4ca87c")
    assert track is not None and track.registration == "EI-FMV" and track.type_code == "A320"
    assert track.classification is not None
    r = await authed.client.get("/api/status")
    assert r.json()["hexdb"]["found"] == 1 and route.call_count == 1
    detail = (await authed.client.get("/api/aircraft/4ca87c")).json()
    assert detail["filled_from"]["type_code"] == "hexdb" and detail["faa"] is None
