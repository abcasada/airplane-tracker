"""Multi-source polling and merge: per-source budgets, merge by hex, Sources report."""

from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.alerts.manager import DISPLAY_FACTS
from app.clock import FakeClock
from app.filters.engine import CompiledFilter
from app.filters.facts import EvalContext, LiveFacts
from app.filters.fields import FIELDS
from app.filters.models import Condition, FilterDefinition, Group
from app.ingest.planner import PlannerConfig, QueryCircle, plan
from app.ingest.poller import FEED_CIRCLE, Poller
from app.ingest.sourcestats import MAX_HEXES_PER_BUCKET, SourceStats
from app.places.models import Place
from app.sources.base import AreaQuery, Observation, SourceBatch, SourceError
from app.tracks.store import HISTORY_MIN_SPACING_S, TrackStore
from tests.conftest import Harness

T = 1_800_000_000.0


def _place(r: float = 10) -> Place:
    return Place(id=1, name="p", lat=38.8, lon=-97.6, radius_nm=r, created_at=0, updated_at=0)


class FakeArea:
    def __init__(self, name: str, rate: float = 100.0, fail: int | None = None) -> None:
        self.name = name
        self.max_rate_per_s = rate
        self.fail = fail
        self.queries: list[AreaQuery] = []

    async def fetch(self, query: AreaQuery) -> SourceBatch:
        self.queries.append(query)
        if self.fail is not None:
            raise SourceError("boom", self.fail)
        obs = Observation(hex="abc", source=self.name, seen_at=T, pos_time=T, lat=38.8, lon=-97.6)
        return SourceBatch(self.name, T, [obs])


class FakeMetered(FakeArea):
    max_cost = 4.0

    def cost(self, query: AreaQuery) -> float:
        return 2.0


class FakeFeed:
    name = "receiver"
    interval_s = 1.0

    def __init__(self) -> None:
        self.calls = 0

    async def fetch_all(self) -> SourceBatch:
        self.calls += 1
        return SourceBatch(self.name, T, [])


async def test_every_source_is_polled_within_its_own_budget() -> None:
    clock = FakeClock(T)
    a, b, broken = FakeArea("a"), FakeArea("b"), FakeArea("c", fail=429)
    feed = FakeFeed()
    got: list[tuple[str, str]] = []

    async def on_batch(batch: SourceBatch, c: QueryCircle) -> None:
        got.append((batch.source, c.tier))

    circles = plan([_place()], 0, PlannerConfig())
    poller = Poller([a, b, broken, feed], clock, lambda: circles, on_batch)
    wait = await poller.tick()
    assert got == [("a", "inner"), ("b", "inner"), ("receiver", "feed")]
    assert wait == pytest.approx(1.0)  # the receiver is read every second
    clock.advance(1)
    await poller.tick()
    assert feed.calls == 2 and len(a.queries) == 1  # area circles aren't due yet
    clock.advance(10)
    await poller.tick()
    assert len(a.queries) == len(b.queries) == 2
    assert len(broken.queries) == 1  # backing off after the 429, without holding others up
    st = {s["name"]: s for s in poller.status()}
    assert st["c"]["healthy"] is False and st["a"]["healthy"] is True
    assert st["receiver"]["kind"] == "receiver" and st["receiver"]["budget_per_s"] is None
    assert st["a"]["budget_per_s"] == 50.0 and st["a"]["used_today"] == 2


async def test_low_budget_source_polls_only_the_enclosing_circle() -> None:
    clock = FakeClock(T)
    slow, metered = FakeArea("slow", rate=0.1), FakeMetered("metered", rate=0.05)

    async def on_batch(_b: SourceBatch, _c: QueryCircle) -> None:
        return None

    circles = plan([_place(5)], 0, PlannerConfig(outer_enabled=True))
    assert {c.tier for c in circles} == {"inner", "outer"}
    poller = Poller([slow, metered], clock, lambda: circles, on_batch)
    assert [c.tier for c in poller.circles_for(poller.slots[0], circles)] == ["outer"]
    await poller.tick()
    assert [q.radius_nm for q in slow.queries] == [85]
    assert len(metered.queries) == 1
    # 2 credits per request from a 4-credit bucket refilling at 0.025 credits/s.
    clock.advance(30)
    await poller.tick()
    assert len(metered.queries) == 2
    clock.advance(30)
    await poller.tick()
    assert len(metered.queries) == 2  # due, but out of credits
    clock.advance(20)
    await poller.tick()
    assert len(metered.queries) == 3
    st = {s["name"]: s for s in poller.status()}
    assert st["metered"]["budget_unit"] == "credit" and st["metered"]["used_today"] == 6


def obs(source: str, t: float, **kw: Any) -> Observation:
    base: dict[str, Any] = dict(
        hex="ae1234", source=source, seen_at=t, pos_time=t, lat=38.8, lon=-97.6, alt_baro_ft=5000
    )
    base.update(kw)
    return Observation(**base)


def test_merge_freshest_position_wins_and_db_fields_fill_in() -> None:
    store = TrackStore()
    store.update(obs("adsb.fi", T, registration="05-5140", type_code="C17", gs_kt=250))
    # A slower source with an older report: position and state stay; missing facts fill in.
    track, _ = store.update(
        obs("opensky", T - 10, lat=39.0, alt_baro_ft=3000, gs_kt=200, owner_op="USAF",
            registration=None, callsign="RCH452")
    )  # fmt: skip
    assert track.lat == 38.8 and track.alt_baro_ft == 5000 and track.gs_kt == 250
    assert track.registration == "05-5140" and track.owner_op == "USAF"
    assert track.callsign == "RCH452"
    assert track.source == "adsb.fi"
    # A fresher report from another source moves it, without clearing what it doesn't carry.
    store.update(obs("receiver", T + 3, lat=38.81, type_code=None))
    assert track.lat == 38.81 and track.type_code == "C17" and track.source == "receiver"
    assert track.sources(T + 3) == ["adsb.fi", "opensky", "receiver"]
    assert track.sources(T + 62) == ["receiver"]


def test_an_older_report_with_a_newer_position_still_moves_the_track() -> None:
    """The receiver hears a non-ADS-B aircraft (Mode S: fresh, no position); an aggregator's
    MLAT fix arrives a few seconds older but is the only, and newest, position."""
    store = TrackStore()
    store.update(obs("adsb.fi", T, pos_source="mlat", gs_kt=300))
    store.update(Observation(hex="ae1234", source="receiver", seen_at=T + 9, alt_baro_ft=5100))
    track, _ = store.update(obs("adsb.fi", T + 5, lat=38.85, pos_source="mlat", gs_kt=310))
    assert track.lat == 38.85 and track.pos_time == T + 5 and track.gs_kt == 310
    assert track.last_seen == T + 9


def test_history_is_spaced_when_reports_arrive_every_second() -> None:
    store = TrackStore()
    for i in range(30):
        track, _ = store.update(obs("receiver", T + i, lat=38.8 + i * 0.001))
    assert len(track.history) == 30 // HISTORY_MIN_SPACING_S + 1
    assert track.lat == pytest.approx(38.829)


def _facts(track_obs: list[Observation], now: float) -> LiveFacts:
    store = TrackStore()
    for o in track_obs:
        track, _ = store.update(o)
    return LiveFacts(track, EvalContext(now, {}, ZoneInfo("UTC")))


def test_sources_fields() -> None:
    f = _facts([obs("adsb.fi", T), obs("receiver-uat", T + 1)], T + 2)
    assert f.get("sources") == ["adsb.fi", "receiver-uat"]
    assert f.get("seen_by_my_receiver") is True
    g = _facts([obs("adsb.fi", T)], T + 2)
    assert g.get("seen_by_my_receiver") is False

    def matches(c: Condition, facts: LiveFacts) -> bool:
        return CompiledFilter(FilterDefinition(root=Group(children=[c])), {}).matches(facts)

    only_aggregators = Condition(field="sources", op="contains_none", value=["receiver"])
    assert matches(only_aggregators, g) and matches(only_aggregators, f)
    assert matches(Condition(field="sources", op="contains_any", value=["receiver-uat"]), f)
    assert matches(Condition(field="seen_by_my_receiver", op="is_false"), g)
    assert FIELDS["sources"].suggest == "sources"
    assert ("sources", None) in DISPLAY_FACTS and ("seen_by_my_receiver", None) in DISPLAY_FACTS
    assert f.snapshot({("sources", None)}) == {"sources": ["adsb.fi", "receiver-uat"]}


def test_source_stats_unique_contribution_and_daily_rollover() -> None:
    s = SourceStats()
    day0 = 20_000 * 86400.0
    s.add("adsb.fi", "a1", day0 + 100)
    s.add("adsb.lol", "a1", day0 + 200)
    s.add("adsb.fi", "a2", day0 + 300)
    s.add("receiver", "a3", day0 + 400)
    r = s.report(day0 + 500, ["adsb.fi", "adsb.lol", "opensky"])
    hour = r["windows"]["last_hour"]
    assert hour["aircraft"] == 3
    assert hour["sources"] == {"adsb.fi": [2, 1], "adsb.lol": [1, 0], "receiver": [1, 1]}
    assert r["sources"] == ["adsb.fi", "adsb.lol", "opensky", "receiver"]
    # An hour later the last-hour window is empty, the 24 h one isn't.
    later = s.report(day0 + 500 + 2 * 3600, [])
    assert later["windows"]["last_hour"]["aircraft"] == 0
    assert later["windows"]["last_24h"]["aircraft"] == 3
    # The next UTC day: yesterday becomes a stored daily total, to be persisted.
    s.add("adsb.fi", "a9", day0 + 86400 + 10)
    assert s.dirty
    assert s.days[20_000]["sources"]["adsb.fi"] == [2, 1]
    restored = SourceStats()
    restored.load(s.to_json())
    days = restored.report(day0 + 86400 + 20, ["adsb.fi"])["days"]
    assert days[0]["day"] == day0 and days[0]["aircraft"] == 3


def test_source_stats_memory_is_bounded() -> None:
    s = SourceStats()
    for i in range(MAX_HEXES_PER_BUCKET + 5):
        s.add("adsb.fi", f"{i:06x}", T)
    assert s.dropped == 5
    s.add("adsb.fi", "zzz", T + 2 * 86400)
    assert list(s.buckets) == [int((T + 2 * 86400) // 900 * 900)]


async def test_sources_report_api_counts_only_polled_areas(authed: Harness) -> None:
    rt = authed.ctx.rt()
    now = authed.clock.now()
    circle = QueryCircle("inner:x", 38.8, -97.6, 30, 10, "inner")
    rt.poller.last_circles = [circle]
    near = Observation(hex="aa0001", source="adsb.fi", seen_at=now, pos_time=now, lat=38.8,
                       lon=-97.6, alt_baro_ft=5000)  # fmt: skip
    far = Observation(hex="aa0002", source="receiver", seen_at=now, pos_time=now, lat=40.0,
                      lon=-97.6, alt_baro_ft=5000)  # fmt: skip
    both = Observation(hex="aa0003", source="adsb.lol", seen_at=now, pos_time=now, lat=38.7,
                       lon=-97.6, alt_baro_ft=5000)  # fmt: skip
    await rt.on_batch(SourceBatch("adsb.fi", now, [near]), circle)
    await rt.on_batch(SourceBatch("receiver", now, [far]), FEED_CIRCLE)
    near.source = "adsb.lol"
    await rt.on_batch(SourceBatch("adsb.lol", now, [both, near]), circle)
    r = (await authed.client.get("/api/sources")).json()
    assert [s["name"] for s in r["status"]] == ["adsb.fi", "adsb.lol"]
    assert r["status"][0]["budget_per_s"] == 0.5
    hour = r["windows"]["last_hour"]
    assert hour["aircraft"] == 2  # the receiver's far aircraft is outside the polled circle
    assert hour["sources"]["adsb.fi"] == [1, 0] and hour["sources"]["adsb.lol"] == [2, 1]
    assert rt.store.get("aa0002") is not None  # still tracked, just not counted
    live = (await authed.client.get("/api/aircraft")).json()["aircraft"]
    assert next(a for a in live if a["hex"] == "aa0001")["sources"] == ["adsb.fi", "adsb.lol"]
    sug = (await authed.client.get("/api/fields/suggest?source=sources&q=rec")).json()
    assert [s["value"] for s in sug] == ["receiver", "receiver-uat"]
    assert sug[0]["label"] == "not configured"


async def test_switch_a_source_off_and_back_on(authed: Harness) -> None:
    c, rt = authed.client, authed.ctx.rt()
    r = await c.put("/api/sources/adsb.lol", json={"enabled": False})
    assert r.status_code == 200, r.text
    status_ = {s["name"]: s["enabled"] for s in (await c.get("/api/sources")).json()["status"]}
    assert status_ == {"adsb.fi": True, "adsb.lol": False}
    assert (await authed.ctx.db.get_json("sources")) == {"disabled": ["adsb.lol"]}
    backup = (await c.get("/api/backup")).json()
    assert backup["settings"]["sources"] == {"disabled": ["adsb.lol"]}
    assert (await c.put("/api/sources/nope", json={"enabled": False})).status_code == 404
    await c.put("/api/sources/adsb.lol", json={"enabled": True})
    assert all(s.enabled for s in rt.poller.slots)
    # Restoring the backup switches it off again.
    assert (await c.post("/api/backup/restore", json=backup)).status_code == 200
    assert [s.enabled for s in rt.poller.slots] == [True, False]
