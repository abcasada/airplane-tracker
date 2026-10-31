"""Take-off/landing detection, its filter fields, notifications and backtest counts."""

import json
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import respx

from app.alerts.manager import AlertingSettings
from app.filters.engine import CompiledFilter
from app.filters.facts import EvalContext, LiveFacts
from app.filters.models import Condition, FilterDefinition, Group, NotifySettings
from app.ingest.poller import WATCH_CIRCLE
from app.places.nearby import Airfield, AirportIndex, _cell
from app.replay.backtest import BacktestEngine, BacktestSpec
from app.replay.core import Cycle
from app.sources.base import Observation, SourceBatch
from app.tracks import events
from app.tracks.store import Track, TrackStore
from tests.conftest import Harness

T0 = 1_800_000_000.0
KSLN = (38.791, -97.652, 1288.0)
KDOV = (39.1295, -75.466, 28.0)


def index(*, heliport: bool = False) -> AirportIndex:
    idx = AirportIndex()
    fields = [Airfield("KSLN", "Salina Regional Airport", *KSLN, "medium_airport"),
              Airfield("KDOV", "Dover Air Force Base", *KDOV, "large_airport")]  # fmt: skip
    if heliport:
        # A hospital helipad 1 nm from the runway.
        fields.append(Airfield("KS99", "Salina Regional Health", 38.805, -97.652, 1250, "heliport"))
    for f in fields:
        idx.grid.setdefault(_cell(f.lat, f.lon), []).append(f)
    return idx


class Flight:
    def __init__(self, hex_: str = "a835af", category: str = "A2") -> None:
        self.store = TrackStore()
        self.hex = hex_
        self.category = category

    def report(self, t: float, lat: float, lon: float, alt: float | None, vrate: float = 0,
               ground: bool = False) -> Track:  # fmt: skip
        obs = Observation(hex=self.hex, source="t", seen_at=t, pos_time=t, lat=lat, lon=lon,
                          alt_baro_ft=None if ground else alt, on_ground=ground, gs_kt=140,
                          track_deg=0, baro_rate_fpm=vrate, category=self.category,
                          registration="N628TS")  # fmt: skip
        return self.store.update(obs)[0]


def run(f: Flight, idx: AirportIndex, now: float) -> list[events.FlightEvent]:
    return [ev for _, ev in events.update(f.store.values(), idx, now)]


def test_observed_takeoff_and_landing() -> None:
    idx, f = index(), Flight()
    f.report(T0, 38.78, -97.652, None, ground=True)
    assert run(f, idx, T0) == []
    f.report(T0 + 10, 38.79, -97.652, 1600, vrate=1500)
    (ev,) = run(f, idx, T0 + 10)
    assert (ev.kind, ev.airport_code, ev.inferred) == ("takeoff", "KSLN", False)
    assert ev.describe() == "took off from Salina Regional Airport (KSLN)"
    f.report(T0 + 20, 38.80, -97.652, 2400, vrate=1500)
    assert run(f, idx, T0 + 20) == []  # one take-off per flight
    f.report(T0 + 3600, 39.12, -75.466, None, ground=True)
    (ev,) = run(f, idx, T0 + 3600)
    assert (ev.kind, ev.airport_code, ev.inferred) == ("landing", "KDOV", False)


def test_inferred_takeoff_needs_low_close_and_climbing() -> None:
    idx = index()
    for alt, vrate, lat, expected in [
        (2500, 1200, 38.80, True),    # 1,200 ft AGL, climbing, ~0.5 nm from KSLN
        (2500, 0, 38.80, False),      # level: just passing
        (6000, 1500, 38.80, False),   # too high
        (2500, 1500, 38.95, False),   # ~9.5 nm out
    ]:  # fmt: skip
        f = Flight()
        f.report(T0, lat, -97.652, alt, vrate=vrate)
        got = run(f, idx, T0)
        assert bool(got) is expected, (alt, vrate, lat)
        if got:
            assert got[0].inferred and got[0].airport_code == "KSLN"


def test_inferred_landing_when_lost_low_near_a_field() -> None:
    idx, f = index(), Flight()
    f.report(T0, 38.70, -97.652, 3500, vrate=-700)
    f.report(T0 + 60, 38.77, -97.652, 1900, vrate=-700)  # ~600 ft AGL, 1.3 nm out
    run(f, idx, T0 + 60)
    assert run(f, idx, T0 + 60 + events.LOST_AFTER_S - 1) == []  # not lost yet
    (ev,) = run(f, idx, T0 + 60 + events.LOST_AFTER_S)
    assert (ev.kind, ev.airport_code, ev.inferred) == ("landing", "KSLN", True)
    assert ev.t == T0 + 60 and ev.describe().endswith("(probably)")
    assert run(f, idx, T0 + 400) == []  # checked once


def test_no_landing_for_overflights_or_climbs() -> None:
    idx = index()
    for alt, vrate in [(5000, -500), (1900, 800)]:
        f = Flight()
        f.report(T0, 38.70, -97.652, alt + 1000, vrate=vrate)
        f.report(T0 + 60, 38.78, -97.652, alt, vrate=vrate)
        run(f, idx, T0 + 60)
        assert run(f, idx, T0 + 400) == [], (alt, vrate)


def test_heliports_only_count_for_rotorcraft() -> None:
    idx = index(heliport=True)
    plane, heli = Flight(), Flight("ae0001", category="A7")
    for f in (plane, heli):
        f.report(T0, 38.805, -97.652, None, ground=True)
        f.report(T0 + 10, 38.806, -97.652, 1500, vrate=800)
    (ev,) = run(plane, idx, T0 + 10)
    assert ev.airport_code == "KSLN"
    (ev,) = run(heli, idx, T0 + 10)
    assert ev.airport_code == "KS99"


def test_fields_expose_recent_events() -> None:
    idx, f = index(), Flight()
    f.report(T0, 38.78, -97.652, None, ground=True)
    track = f.report(T0 + 10, 38.79, -97.652, 1600, vrate=1500)
    run(f, idx, T0 + 10)
    facts = LiveFacts(track, EvalContext(T0 + 20, {}, ZoneInfo("UTC")))
    assert facts.get("took_off_recently") is True and facts.get("takeoff_airport") == "KSLN"
    assert facts.get("landed_recently") is False and facts.get("landing_airport") is None
    later = LiveFacts(track, EvalContext(T0 + 10 + events.RECENT_S + 1, {}, ZoneInfo("UTC")))
    assert later.get("took_off_recently") is False


NTFY = "http://ntfy"


async def _setup(h: Harness, on_match: bool) -> None:
    db = h.ctx.db
    await db.execute(
        "INSERT INTO aircraft_db(icao, reg, type_code, descr) VALUES "
        "('a835af', 'N628TS', 'GLF6', 'GULFSTREAM G650')"
    )
    await db.executemany(
        "INSERT INTO airports(ident, icao, name, type, lat, lon, elevation_ft) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [("KSLN", "KSLN", "Salina Regional Airport", "medium_airport", *KSLN),
         ("KDOV", "KDOV", "Dover Air Force Base", "large_airport", *KDOV)],
    )  # fmt: skip
    await h.ctx.rt().airport_index.load(db)
    c = h.client
    await c.put("/api/settings/ntfy", json={"url": NTFY, "topic": "planes"})
    body = {
        "name": "Follow N628TS",
        "definition": {
            "root": {"mode": "all", "children": [
                {"type": "cond", "field": "registration", "op": "in", "value": ["N628TS"]},
            ]},
            "notify": {"on_match": on_match, "on_takeoff": True, "on_landing": True},
        },
    }  # fmt: skip
    assert (await c.post("/api/filters", json=body)).status_code == 201


async def _feed(h: Harness, **kw: Any) -> None:
    rt = h.ctx.rt()
    now = h.clock.now()
    obs = []
    if kw:
        obs.append(Observation(hex="a835af", source="adsb.fi", seen_at=now, pos_time=now,
                               registration="N628TS", type_code="GLF6", gs_kt=150, track_deg=0,
                               **kw))  # fmt: skip
    await rt.on_batch(SourceBatch("adsb.fi", now, obs), WATCH_CIRCLE)


@respx.mock
async def test_takeoff_and_landing_notifications(authed: Harness) -> None:
    await _setup(authed, on_match=False)
    sent = respx.post(f"{NTFY}/").mock(return_value=httpx.Response(200, json={}))
    # First heard climbing out of Salina: the new episode is announced as a take-off.
    await _feed(authed, lat=38.80, lon=-97.652, alt_baro_ft=2600, baro_rate_fpm=1800)
    assert sent.call_count == 1
    first = json.loads(sent.calls[0].request.content)
    assert (
        first["title"] == "🛫 N628TS GULFSTREAM G650 took off from Salina Regional Airport (KSLN)"
    )
    assert first["sequence_id"] == "aircraft-a835af"
    # Cruise: still the same episode, nothing new.
    authed.clock.advance(60)
    await _feed(authed, lat=39.5, lon=-90.0, alt_baro_ft=41000)
    assert sent.call_count == 1
    # Last heard low on approach to Dover, then lost.
    authed.clock.advance(60)
    await _feed(authed, lat=39.11, lon=-75.466, alt_baro_ft=700, baro_rate_fpm=-700)
    for _ in range(4):
        authed.clock.advance(40)
        await _feed(authed)  # other polls keep the loop running
    assert sent.call_count == 2
    landing = json.loads(sent.calls[1].request.content)
    assert landing["title"] == (
        "🛬 N628TS GULFSTREAM G650 landed at Dover Air Force Base (KDOV) (probably)"
    )
    alert = (await authed.client.get("/api/alerts")).json()[0]
    assert alert["notified"] == 1
    detail = (await authed.client.get(f"/api/alerts/{alert['id']}")).json()
    assert [n["kind"] for n in detail["notifications"]] == ["takeoff", "landing"]


@respx.mock
async def test_match_only_filters_ignore_takeoffs(authed: Harness) -> None:
    await _setup(authed, on_match=True)
    rt = authed.ctx.rt()
    flt = rt.filters[-1]
    await authed.client.put(f"/api/filters/{flt.id}", json={
        "name": flt.name,
        "definition": flt.definition.model_dump() | {"notify": {"on_match": True}},
    })  # fmt: skip
    sent = respx.post(f"{NTFY}/").mock(return_value=httpx.Response(200, json={}))
    await _feed(authed, lat=38.80, lon=-97.652, alt_baro_ft=2600, baro_rate_fpm=1800)
    title = json.loads(sent.calls[0].request.content)["title"]
    assert not title.startswith("🛫")
    authed.clock.advance(60)
    await _feed(authed, lat=39.11, lon=-75.466, alt_baro_ft=700, baro_rate_fpm=-700)
    for _ in range(5):
        authed.clock.advance(40)
        await _feed(authed)
    assert sent.call_count == 1


def test_backtest_counts_takeoff_and_landing_notifications() -> None:
    def engine(n: NotifySettings) -> BacktestEngine:
        d = FilterDefinition(
            root=Group(children=[Condition(field="registration", op="in", value=["N628TS"])]),
            notify=n,
        )
        spec = BacktestSpec(d, None, 1, "live", T0, T0 + 3600)
        return BacktestEngine(spec, CompiledFilter(d, {}), None, [], AlertingSettings(),
                              ZoneInfo("UTC"), T0)  # fmt: skip

    f = Flight()
    track = f.report(T0, 38.80, -97.652, 2600, vrate=1800)
    ctx = EvalContext(T0, {}, ZoneInfo("UTC"))
    takeoff = events.FlightEvent("takeoff", T0, "KSLN", "Salina", True)
    landing = events.FlightEvent("landing", T0 + 600, "KDOV", "Dover", True)
    cycles = [
        Cycle(T0, [LiveFacts(track, ctx)], [(track, takeoff)]),
        Cycle(T0 + 600, [LiveFacts(track, EvalContext(T0 + 600, {}, ZoneInfo("UTC")))],
              [(track, landing)]),
    ]  # fmt: skip
    for notify, expected in [
        (NotifySettings(), 1),  # match only
        (NotifySettings(on_match=False, on_takeoff=True, on_landing=True), 2),
        (NotifySettings(on_takeoff=True, on_landing=True), 2),  # take-off rides on the match
        (NotifySettings(on_match=False), 0),
    ]:
        e = engine(notify)
        for c in cycles:
            e.observe(c)
        assert e.result()["totals"]["draft"]["notified"] == expected, notify
