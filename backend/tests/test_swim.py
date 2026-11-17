"""FAA SWIM (SCDS TFMS) schedule source: parsing, filtering, storage, fields and the bridge."""

import asyncio
import contextlib
import gzip
import threading
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.clock import FakeClock
from app.db import Database
from app.filters.facts import LiveFacts
from app.ingest import swim as swim_ingest
from app.ingest import swimstore
from app.ingest.swim import SwimFeed, swim_config
from app.ingest.swimstore import FiledFlight, SwimFlights
from app.main import create_app
from app.sources.base import Observation, SourceBatch, SourceError
from app.sources.swim import (
    FlightUpdate,
    SolaceConnector,
    SwimConfig,
    airport_aliases,
    decode_payload,
    interest_pattern,
    normalize_acid,
    parse_payload,
    parse_time,
)
from tests.conftest import Harness, make_settings

FIXTURES = Path(__file__).parent / "fixtures" / "swim"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def ts(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()


# ---- parsing ------------------------------------------------------------------------------


def test_parses_a_flight_plan_with_prefixed_namespaces() -> None:
    r = parse_payload(fixture("flight_plan.xml"))
    assert r.messages == 2 and r.errors == 0 and r.skipped == 0
    u = r.updates[0]
    assert u.key == "ref:100001"
    assert u.acid == "TST101"
    assert (u.origin, u.destination) == ("KDEN", "KICT")
    assert u.gufi == "TST0000001"
    assert u.aircraft_type == "B763"  # from "H/B763/L"
    assert u.scheduled_departure == ts("2027-01-15T14:00:00")
    assert (u.etd, u.etd_type) == (ts("2027-01-15T14:10:00"), "PROPOSED")
    assert (u.eta, u.eta_type) == (ts("2027-01-15T15:25:00"), "ESTIMATED")
    assert u.sent_at == ts("2027-01-15T12:00:00")
    assert not u.cancelled and u.departed_at is None
    assert r.updates[1].aircraft_type == "A320"


def test_parses_default_namespace_faa_codes_and_status() -> None:
    r = parse_payload(fixture("modify_default_ns.xml"))
    u = r.updates[0]
    assert (u.origin, u.destination) == ("DEN", "ICT")
    assert u.status == "SCHEDULED"
    assert u.aircraft_type == "B763"
    assert u.msg_type == "FlightModify"
    assert u.sent_at == ts("2027-01-15T13:30:00.250")
    assert (u.etd, u.eta) == (ts("2027-01-15T14:20:00"), ts("2027-01-15T15:35:00"))


def test_parses_departure_arrival_and_cancellation() -> None:
    r = parse_payload(fixture("departure_arrival.xml"))
    dep, cancel, arr = r.updates
    assert dep.departed_at == ts("2027-01-15T14:21:00") and dep.etd_type == "ACTUAL"
    assert cancel.cancelled and cancel.acid == "N123TS"
    assert arr.arrived_at == ts("2027-01-15T15:32:00")


def test_flow_information_and_junk_are_ignored() -> None:
    r = parse_payload(fixture("flow_info.xml"))
    assert r.updates == [] and r.messages == 0 and r.errors == 0
    assert parse_payload(b"<not xml").errors == 1
    assert parse_payload(b"").errors == 1
    assert parse_payload(None).errors == 1


def test_gzip_payloads_are_inflated_and_oversized_dropped() -> None:
    raw = fixture("flight_plan.xml")
    assert decode_payload(gzip.compress(raw)) == raw
    assert decode_payload("<a/>") == b"<a/>"
    assert decode_payload(gzip.compress(b" " * 3_000_000)) is None
    assert len(parse_payload(gzip.compress(raw)).updates) == 2


def test_wanted_skips_message_bodies() -> None:
    seen: list[tuple[str | None, str | None, str]] = []

    def wanted(dep: str | None, arr: str | None, acid: str) -> bool:
        seen.append((dep, arr, acid))
        return "KICT" in (dep, arr)

    r = parse_payload(fixture("flight_plan.xml"), wanted)
    assert [u.acid for u in r.updates] == ["TST101"]
    assert r.skipped == 1
    assert seen == [("KDEN", "KICT", "TST101"), ("KBOS", "KJFK", "OTH200")]


def test_interest_pattern_matches_headers_by_icao_or_faa_code() -> None:
    p = interest_pattern({"KICT"}, set())
    assert p is not None
    assert p.search(fixture("flight_plan.xml"))
    assert p.search(fixture("modify_default_ns.xml"))  # arrArpt="ICT"
    assert not p.search(fixture("flow_info.xml"))
    by_reg = interest_pattern(set(), {"N123TS"})
    assert by_reg is not None and by_reg.search(fixture("departure_arrival.xml"))
    assert interest_pattern(set(), set()) is None


def test_small_helpers() -> None:
    assert airport_aliases("KICT") == {"KICT", "ICT"}
    assert airport_aliases("PANC") == {"PANC", "ANC"}
    assert airport_aliases("EGLL") == {"EGLL"}
    assert normalize_acid(" n-123ts ") == "N123TS"
    assert normalize_acid("bad!") is None
    assert parse_time("2027-01-15T14:00:00Z") == ts("2027-01-15T14:00:00")
    assert parse_time("2027-01-15T14:00:00") is None  # no zone: refuse rather than guess
    assert parse_time("soon") is None


# ---- store: merge, matching, retention, persistence ----------------------------------------

T0 = ts("2027-01-15T12:00:00")


AIRPORT_ROWS = [
    ("KDEN", "KDEN", "Test Denver", "large_airport", 39.86, -104.67),
    ("KICT", "KICT", "Test Wichita", "large_airport", 37.65, -97.43),
    ("KAAO", "KAAO", "Test Jabara", "small_airport", 37.75, -97.22),
]


async def add_airports(db: Database) -> None:
    await db.executemany(
        "INSERT OR REPLACE INTO airports(ident, icao, name, type, lat, lon) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        AIRPORT_ROWS,
    )


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "db.sqlite3")
    await database.connect()
    await add_airports(database)
    yield database
    await database.close()


async def make_store(db: Database) -> SwimFlights:
    return SwimFlights(db)


def update(**kw: object) -> FlightUpdate:
    base: dict[str, object] = {"key": "ref:1", "acid": "TST101", "msg_type": "x", "sent_at": T0}
    return FlightUpdate(**{**base, **kw})  # type: ignore[arg-type]


def key_of(f: FiledFlight | None) -> str | None:
    return f.key if f else None


async def test_store_merges_messages_and_resolves_faa_codes(db: Database) -> None:
    store = await make_store(db)
    for name in ("flight_plan.xml", "modify_default_ns.xml", "departure_arrival.xml"):
        await store.apply(parse_payload(fixture(name)).updates)
    f = store.flights["ref:100001"]
    assert (f.origin, f.destination) == ("KDEN", "KICT")  # "DEN"/"ICT" resolved
    assert f.aircraft_type == "B763" and f.status == "SCHEDULED"
    assert f.departed_at == ts("2027-01-15T14:21:00")
    assert f.arrived_at == ts("2027-01-15T15:32:00")
    assert f.departure_time == f.departed_at and f.arrival_time == f.arrived_at
    assert store.flights["ref:100003"].cancelled
    assert store.airports["DEN"] == store.airports["KDEN"]


async def test_late_messages_only_fill_gaps(db: Database) -> None:
    store = await make_store(db)
    await store.apply([update(eta=T0 + 7200, sent_at=T0 + 60)])
    changed = await store.apply([update(eta=T0 + 3600, aircraft_type="B738", sent_at=T0)])
    f = store.flights["ref:1"]
    assert f.eta == T0 + 7200  # the older message doesn't overwrite
    assert f.aircraft_type == "B738"  # but fills what was missing
    assert changed == [f]
    assert await store.apply([update(eta=T0 + 7200, sent_at=T0 + 120)]) == []


async def test_best_for_picks_the_flight_in_progress(db: Database) -> None:
    store = await make_store(db)
    await store.apply([
        update(key="ref:1", etd=T0, eta=T0 + 3600, arrived_at=T0 + 3500, sent_at=T0 + 3500),
        update(key="ref:2", etd=T0 + 5400, eta=T0 + 9000),
        update(key="ref:3", etd=T0 + 7200, eta=T0 + 9000, cancelled=True),
        update(key="ref:4", acid="N123TS", etd=T0, eta=T0 + 1800),
    ])  # fmt: skip
    assert key_of(store.best_for("TST101", None, T0 + 600)) == "ref:1"
    assert key_of(store.best_for("TST101", None, T0 + 3700)) == "ref:1"
    # After the first leg's arrival + 30 min only the next leg fits.
    assert key_of(store.best_for("TST101", None, T0 + 5500)) == "ref:2"
    assert store.best_for("TST101", None, T0 - 3 * 3600) is None
    # General aviation: the plan is filed under the registration.
    assert key_of(store.best_for("N123TS", None, T0)) == "ref:4"
    assert key_of(store.best_for(None, "N-123TS", T0)) == "ref:4"
    assert store.best_for(None, None, T0) is None


async def test_route_from_a_filed_plan(db: Database) -> None:
    store = await make_store(db)
    await store.apply([update(origin="DEN", destination="KICT", etd=T0, eta=T0 + 4000)])
    route = store.route_for(store.flights["ref:1"])
    assert route is not None and route.known and route.source == "faa-swim"
    assert [leg.icao for leg in route.legs] == ["KDEN", "KICT"]
    await store.apply([update(key="ref:9", origin="ZZZZ", destination="KICT", etd=T0)])
    assert store.route_for(store.flights["ref:9"]) is None  # unknown airport: no legs


async def test_store_persists_prunes_and_stays_bounded(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await make_store(db)
    await store.apply([update(key="ref:1", etd=T0, eta=T0 + 3600, origin="KDEN")])
    await store.apply([update(key="ref:2", etd=T0 - 5 * 86400, sent_at=T0 - 5 * 86400)])
    assert await store.flush() == 2
    again = SwimFlights(store.db)
    await again.load(T0)
    assert set(again.flights) == {"ref:1"}  # older than the retention isn't loaded
    assert "KDEN" in again.airports
    assert await store.prune(T0) == 1
    assert set(store.flights) == {"ref:1"}
    rows = await store.db.fetchall("SELECT key FROM swim_flights")
    assert [r["key"] for r in rows] == ["ref:1"]
    monkeypatch.setattr(swimstore, "MAX_FLIGHTS", 10)
    await store.apply([update(key=f"ref:x{i}", etd=T0 + i, sent_at=T0 + i) for i in range(20)])
    assert len(store.flights) <= 10 and store.evicted > 0
    assert "ref:x19" in store.flights
    assert sum(len(v) for v in store.by_acid.values()) == len(store.flights)


async def test_upcoming_lists_flights_for_your_airports(db: Database) -> None:
    store = await make_store(db)
    await store.apply([
        update(key="ref:1", origin="KDEN", destination="KICT", etd=T0 + 600, eta=T0 + 4000),
        update(key="ref:2", origin="KAAO", destination="ICT", etd=T0 + 60, eta=T0 + 900),
        update(key="ref:3", origin="KDEN", destination="KAAO", etd=T0, eta=T0 + 3600),
    ])  # fmt: skip
    assert [f.key for f in store.upcoming({"KICT"}, T0)] == ["ref:2", "ref:1"]


# ---- bridge: Solace thread → bounded queue → asyncio ---------------------------------------


class FakeConnector:
    def __init__(self, cfg: SwimConfig, on_payload: Callable[[bytes], None]) -> None:
        self.on_payload = on_payload
        self.connected = False
        self.fail = 0  # connect attempts that raise
        self.attempts = 0
        self.closed = 0

    def connect(self) -> None:
        self.attempts += 1
        if self.fail:
            self.fail -= 1
            raise RuntimeError("broker unreachable")
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    def close(self) -> None:
        self.connected = False
        self.closed += 1


CFG = SwimConfig("tcps://scds.example.invalid:55443", "TFMS", "user", "secret", "queue")


async def make_feed(db: Database) -> tuple[SwimFeed, list[list[FiledFlight]], FakeClock]:
    store = await make_store(db)
    got: list[list[FiledFlight]] = []

    async def on_flights(flights: list[FiledFlight]) -> None:
        got.append(flights)

    clock = FakeClock(T0)
    return SwimFeed(CFG, clock, store, on_flights, FakeConnector), got, clock


async def test_offer_keeps_only_your_airports_and_aircraft(db: Database) -> None:
    feed, got, _ = await make_feed(db)
    feed.offer(fixture("flight_plan.xml"))
    assert feed.irrelevant == 1  # no interest set yet
    feed.set_interest({"KICT"}, set())
    feed.offer(fixture("flight_plan.xml"))
    feed.offer(fixture("flow_info.xml"))
    feed.offer(gzip.compress(fixture("modify_default_ns.xml")))
    feed.offer(b"\x1f\x8bnot gzip")
    assert (feed.received, feed.kept, feed.irrelevant, feed.dropped) == (5, 2, 2, 1)
    assert await feed.drain() == 1
    assert list(feed.store.flights) == ["ref:100001"]  # OTH200 (KBOS→KJFK) skipped
    assert [f.acid for f in got[0]] == ["TST101"]
    st = feed.status()
    assert st["kind"] == "schedule" and st["swim"]["kept"] == 2
    assert st["swim"]["flights"] == 1 and st["swim"]["queued"] == 0
    assert st["swim"]["messages_per_min"] == 0  # the current minute isn't complete yet


async def test_queue_is_bounded_and_drops_the_oldest(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed, _, _ = await make_feed(db)
    monkeypatch.setattr(swim_ingest, "QUEUE_MAX", 3)
    feed.set_interest({"KICT"}, set())
    plan = fixture("flight_plan.xml")
    for i in range(5):
        feed.offer(plan.replace(b"100001", str(200000 + i).encode()))
    assert feed.dropped == 2 and len(feed._queue) == 3
    await feed.drain()
    assert "ref:200000" not in feed.store.flights and "ref:200004" in feed.store.flights


async def test_offer_from_another_thread_wakes_the_consumer(db: Database) -> None:
    feed, got, _ = await make_feed(db)
    feed.set_interest(set(), {"N123TS"})
    task = asyncio.create_task(feed.run())
    try:
        for _ in range(200):
            if feed.state == "connected":
                break
            await asyncio.sleep(0.01)
        thread = threading.Thread(target=feed.offer, args=(fixture("departure_arrival.xml"),))
        thread.start()
        thread.join()
        for _ in range(200):
            if got:
                break
            await asyncio.sleep(0.01)
        assert [f.acid for f in got[0]] == ["N123TS"]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert isinstance(feed.connector, FakeConnector) and feed.connector.closed >= 1


async def test_reconnects_with_exponential_backoff(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed, _, clock = await make_feed(db)
    monkeypatch.setattr(swim_ingest, "BACKOFF_MIN_S", 0.01)
    monkeypatch.setattr(swim_ingest, "CHECK_INTERVAL_S", 0.01)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def sleep(s: float) -> None:
        sleeps.append(s)
        await real_sleep(0)

    monkeypatch.setattr("app.ingest.swim.asyncio.sleep", sleep)
    conn = feed.connector
    assert isinstance(conn, FakeConnector)
    conn.fail = 3

    async def until(cond: Callable[[], bool]) -> None:
        for _ in range(2000):
            if cond():
                return
            await real_sleep(0)
        raise AssertionError("timed out")

    task = asyncio.create_task(feed._connection_loop())
    try:
        await until(lambda: feed.state == "connected")
        assert conn.attempts == 4
        assert sleeps[:3] == [0.01, 0.02, 0.04]
        assert feed.errors == 3 and feed.last_error is None
        conn.connected = False  # the broker drops us
        await until(lambda: feed.state != "connected")
        await until(lambda: feed.last_error is not None)
        assert "disconnected" in (feed.last_error or "")
        await until(lambda: feed.state == "connected")
        feed.set_enabled(False)
        await until(lambda: feed.state == "off")
        assert not conn.connected
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert clock.now() == T0  # waits are asyncio sleeps, never the wall clock


def test_swim_is_configured_only_when_every_setting_is_present(tmp_path: Path) -> None:
    assert swim_config(make_settings(tmp_path)) is None
    partial = make_settings(tmp_path, swim_host="tcps://h:55443", swim_vpn="TFMS")
    assert swim_config(partial) is None
    full = make_settings(
        tmp_path, swim_host="tcps://h:55443", swim_vpn="TFMS", swim_username="u",
        swim_password="p", swim_queue="q",
    )  # fmt: skip
    cfg = swim_config(full)
    assert cfg is not None and cfg.queue == "q" and cfg.trust_store_dir == "/etc/ssl/certs"


def test_connector_reports_a_missing_library(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_solace(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr("app.sources.swim.importlib.import_module", no_solace)
    assert not SolaceConnector.available()
    conn = SolaceConnector(CFG, lambda _b: None)
    with pytest.raises(SourceError, match="not installed"):
        conn.connect()
    assert not conn.is_connected()
    conn.close()  # nothing to close: no error


# ---- end to end: fields, routes, Sources report, switch ------------------------------------

SWIM_ENV = {
    "swim_host": "tcps://scds.example.invalid:55443", "swim_vpn": "TFMS",
    "swim_username": "user", "swim_password": "secret", "swim_queue": "queue",
}  # fmt: skip
ICT = {"name": "Test field", "role": "airport", "airport_icao": "KICT", "lat": 37.65,
       "lon": -97.43, "elevation_ft": 1333, "radius_nm": 15}  # fmt: skip


@pytest.fixture
async def swim_app(tmp_path: Path) -> AsyncIterator[Harness]:
    clock = FakeClock(ts("2027-01-15T14:50:00"))  # the test flight is airborne
    app = create_app(make_settings(tmp_path, **SWIM_ENV), clock)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            h = Harness(app, client, clock, app.state.ctx)
            await h.setup_account()
            await add_airports(h.ctx.db)
            yield h


def airliner(now: float, **kw: object) -> Observation:
    base: dict[str, object] = dict(
        hex="a0b1c2", source="adsb.fi", seen_at=now, pos_time=now, lat=38.6, lon=-100.5,
        alt_baro_ft=37000, gs_kt=450, track_deg=100, callsign="TST101", category="A5",
    )  # fmt: skip
    base.update(kw)
    return Observation(**base)  # type: ignore[arg-type]


async def test_filed_plan_fields_routes_and_status(swim_app: Harness) -> None:
    h = swim_app
    rt = h.ctx.rt()
    assert rt.swim is not None
    place_id = (await h.client.post("/api/places", json=ICT)).json()["id"]
    rt.swim.offer(fixture("flight_plan.xml"))  # KICT is a place now: kept
    rt.swim.offer(fixture("modify_default_ns.xml"))  # arrArpt="ICT": FAA code
    await rt.swim.drain()
    now = h.clock.now()
    await rt.ingest(SourceBatch("adsb.fi", now, [airliner(now)]))
    t = rt.store.get("a0b1c2")
    assert t is not None and t.route is not None and t.route.source == "faa-swim"
    facts = LiveFacts(t, rt.eval_context())
    assert facts.get("has_filed_plan") is True
    assert facts.get("filed_origin") == "KDEN" and facts.get("filed_destination") == "KICT"
    assert facts.get("filed_aircraft_type") == "B763"
    assert facts.get("filed_status") == "SCHEDULED"
    assert facts.get("filed_departure_in_s") == ts("2027-01-15T14:20:00") - now
    assert facts.get("filed_arrival_in_s") == ts("2027-01-15T15:35:00") - now
    assert facts.get("filed_to_place", place_id) is True
    assert facts.get("filed_from_place", place_id) is False
    assert facts.get("filed_eta_s", place_id) == ts("2027-01-15T15:35:00") - now
    assert facts.get("route_destination") == "KICT" and facts.get("destined_for", place_id)
    assert "TST101" not in rt.routes.pending  # adsb.im isn't asked when a plan is known

    # A different callsign has no plan; an unknown is not the same as "no plan".
    await rt.ingest(SourceBatch("adsb.fi", now, [airliner(now, hex="a0b1c3", callsign="XYZ9")]))
    other = rt.store.get("a0b1c3")
    assert other is not None
    assert LiveFacts(other, rt.eval_context()).get("has_filed_plan") is False

    # Filters can use the fields, and alerts keep them for "why did this fire?".
    definition = {"root": {"mode": "all", "children": [
        {"type": "cond", "id": "a", "field": "filed_to_place", "op": "is_true",
         "place": place_id},
        {"type": "cond", "id": "b", "field": "filed_aircraft_type", "op": "in", "value": ["B763"]},
    ]}}  # fmt: skip
    r = (await h.client.post("/api/filters/preview", json={"definition": definition})).json()
    assert r["errors"] == [] and [m["hex"] for m in r["matches"]] == ["a0b1c2"]

    upcoming = (await h.client.get("/api/sources/swim/flights")).json()
    assert upcoming["configured"] is True
    assert [f["acid"] for f in upcoming["flights"]] == ["TST101"]

    report = (await h.client.get("/api/sources")).json()
    swim = next(s for s in report["status"] if s["name"] == "faa-swim")
    assert swim["kind"] == "schedule" and swim["swim"]["kept"] == 2
    assert "faa-swim" in report["sources"]
    r2 = await h.client.put("/api/sources/faa-swim", json={"enabled": False})
    assert r2.status_code == 200
    assert rt.swim.enabled is False
    await h.client.put("/api/sources/faa-swim", json={"enabled": True})
    assert rt.swim.enabled is True


async def test_followed_registrations_are_kept(swim_app: Harness) -> None:
    h = swim_app
    rt = h.ctx.rt()
    assert rt.swim is not None
    body = {"name": "Follow", "definition": {"root": {"mode": "all", "children": [
        {"type": "cond", "id": "a", "field": "registration", "op": "in", "value": ["N-123TS"]},
    ]}}}  # fmt: skip
    assert (await h.client.post("/api/filters", json=body)).status_code == 201
    rt.swim.offer(fixture("departure_arrival.xml"))  # N123TS: no airport place, but followed
    await rt.swim.drain()
    assert "ref:100003" in rt.swim_flights.flights
    assert "ref:100001" not in rt.swim_flights.flights


async def test_fields_are_unknown_without_swim(authed: Harness) -> None:
    rt = authed.ctx.rt()
    assert rt.swim is None
    now = authed.clock.now()
    await rt.ingest(SourceBatch("adsb.fi", now, [airliner(now)]))
    t = rt.store.get("a0b1c2")
    assert t is not None
    facts = LiveFacts(t, rt.eval_context())
    assert facts.get("has_filed_plan") is None and facts.get("filed_origin") is None
    status = (await authed.client.get("/api/sources")).json()["status"]
    assert "faa-swim" not in [s["name"] for s in status]
    assert (
        await authed.client.put("/api/sources/faa-swim", json={"enabled": False})
    ).status_code == 404
