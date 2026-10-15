from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.clock import FakeClock
from app.ingest.planner import PlannerConfig, merge_circles, plan
from app.ingest.poller import Poller
from app.ingest.ratelimit import CircuitBreaker, TokenBucket
from app.ingest.recorder import Recorder, decode, encode, read_batches
from app.places.models import Place
from app.sources.base import AreaQuery, Observation, SourceBatch, SourceError
from app.sources.readsb import adsbfi, parse_payload
from app.tracks.store import TrackStore

NOW_MS = 1_800_000_000_000


def sample_payload() -> dict[str, Any]:
    """Shape copied from a real readsb/adsb.fi response; values are synthetic."""
    return {
        "now": NOW_MS,
        "total": 3,
        "ac": [
            {
                "hex": "ADFEB7", "type": "adsb_icao", "flight": "SAM491  ", "r": "98-0001",
                "t": "B752", "dbFlags": 1, "alt_baro": 35025, "alt_geom": 35500, "gs": 512.1,
                "track": 271.2, "baro_rate": 0, "squawk": "3372", "emergency": "none",
                "category": "A4", "nav_heading": 270.0, "nav_altitude_mcp": 35008,
                "lat": 38.9, "lon": -97.1, "seen_pos": 0.5, "seen": 0.2, "nac_p": 10,
            },
            {
                "hex": "a4aebe", "type": "mlat", "flight": "N400XR", "alt_baro": "ground",
                "lat": 38.79, "lon": -97.65, "seen_pos": 2.0, "seen": 1.0,
            },
            {"hex": "~2a1b3c", "type": "tisb_other", "seen": 5.0},
            {"hex": "ab0000", "lat": 38.0, "lon": -97.0, "seen_pos": 120.0, "seen": 1.0},
        ],
    }  # fmt: skip


def test_parse_readsb_payload() -> None:
    batch = parse_payload(sample_payload(), "adsb.fi", 0)
    assert batch.fetched_at == pytest.approx(NOW_MS / 1000)
    by_hex = {o.hex: o for o in batch.observations}
    sam = by_hex["adfeb7"]
    assert sam.callsign == "SAM491"
    assert sam.db_flags == 1
    assert sam.registration == "98-0001"
    assert sam.pos_time == pytest.approx(NOW_MS / 1000 - 0.5)
    assert sam.emergency is None
    assert sam.nav_altitude_ft == 35008
    assert sam.pos_source == "adsb"
    ground = by_hex["a4aebe"]
    assert ground.on_ground and ground.alt_baro_ft is None
    assert ground.pos_source == "mlat"
    assert by_hex["~2a1b3c"].lat is None
    assert by_hex["ab0000"].lat is None  # stale position dropped


@respx.mock
async def test_adsbfi_source_fetch_and_errors() -> None:
    async with httpx.AsyncClient() as http:
        src = adsbfi(http, FakeClock())
        route = respx.get("https://opendata.adsb.fi/api/v3/lat/38.79/lon/-97.65/dist/35")
        route.mock(return_value=httpx.Response(200, json=sample_payload()))
        batch = await src.fetch(AreaQuery(38.79, -97.65, 34.2))
        assert len(batch.observations) == 4
        route.mock(return_value=httpx.Response(429))
        with pytest.raises(SourceError) as e:
            await src.fetch(AreaQuery(38.79, -97.65, 34.2))
        assert e.value.is_client_error


def _place(lat: float, lon: float, r: float, **kw: Any) -> Place:
    return Place(id=1, name="p", lat=lat, lon=lon, radius_nm=r, created_at=0, updated_at=0, **kw)


def test_planner_merges_nearby_places() -> None:
    home = _place(38.84, -97.61, 10)
    airport = _place(38.791, -97.652, 10, role="airport")
    far = _place(39.9, -105.0, 10)
    circles = plan([home, airport, far], 0, PlannerConfig())
    assert len(circles) == 2
    salina = min(circles, key=lambda c: abs(c.lat - 38.8))
    assert salina.radius_nm <= 35 * 1.15 + 1


def test_planner_skips_inactive_places_and_adds_outer_tier() -> None:
    inactive = _place(38.8, -97.6, 10, active_until=100.0)
    assert plan([inactive], 200, PlannerConfig()) == []
    circles = plan([_place(38.8, -97.6, 5)], 0, PlannerConfig(outer_enabled=True))
    tiers = {c.tier: c for c in circles}
    assert tiers["outer"].radius_nm == 85 and tiers["outer"].interval_s == 30


def test_merge_keeps_contained_circle_out() -> None:
    merged = merge_circles([(38.8, -97.6, 50), (38.85, -97.6, 5)])
    assert merged == [(38.8, -97.6, 50)]


def test_token_bucket() -> None:
    b = TokenBucket(0.5)
    assert b.take(0.0)
    assert not b.take(1.0)
    assert b.wait_time(1.0) == pytest.approx(1.0)
    assert b.take(2.0)


def test_breaker_opens_hard_on_client_errors() -> None:
    br = CircuitBreaker()
    br.failure(0, "HTTP 429", client_error=True)
    assert br.is_open(10) and not br.is_open(31)
    br2 = CircuitBreaker()
    br2.failure(0, "timeout", client_error=False)
    br2.failure(0, "timeout", client_error=False)
    assert not br2.is_open(0.1)
    br2.failure(0, "timeout", client_error=False)
    assert br2.is_open(1)


class FakeSource:
    def __init__(self, name: str, fail: int | None = None) -> None:
        self.name = name
        self.max_rate_per_s = 100.0
        self.fail = fail
        self.calls = 0

    async def fetch(self, query: AreaQuery) -> SourceBatch:
        self.calls += 1
        if self.fail is not None:
            raise SourceError("boom", self.fail)
        return SourceBatch(self.name, 0, [Observation(hex="abc", source=self.name, seen_at=0)])


async def test_poller_fails_over_and_respects_intervals() -> None:
    clock = FakeClock(1000)
    primary, backup = FakeSource("a", fail=429), FakeSource("b")
    got: list[str] = []

    async def on_batch(batch: SourceBatch, _c: Any) -> None:
        got.append(batch.source)

    circles = plan([_place(38.8, -97.6, 10)], 0, PlannerConfig())
    poller = Poller([primary, backup], clock, lambda: circles, on_batch)
    wait = await poller.tick()
    assert got == ["b"]
    assert wait == pytest.approx(10.0)
    assert await poller.tick() > 0 and got == ["b"]  # not due yet
    clock.advance(10)
    await poller.tick()
    assert primary.calls == 1  # breaker still open, primary skipped
    assert got == ["b", "b"]


def test_track_squawk_streak_and_history() -> None:
    store = TrackStore()
    for i, sq in enumerate(["7700", "7700", "1200"]):
        obs = Observation(hex="abc", source="x", seen_at=100 + i * 10, pos_time=100 + i * 10,
                          lat=38.0 + i * 0.01, lon=-97.0, squawk=sq, alt_baro_ft=5000)  # fmt: skip
        track, _ = store.update(obs)
        if i == 1:
            assert track.squawk_streak == 2
    assert track.squawk == "1200" and track.squawk_streak == 1
    assert len(track.history) == 3
    assert store.expire(1000) == ["abc"]


def test_recorder_roundtrip(tmp_path: Path) -> None:
    rec = Recorder(tmp_path)
    batch = parse_payload(sample_payload(), "adsb.fi", 0)
    rec.write(batch)
    batch2 = SourceBatch("adsb.fi", batch.fetched_at + 10, batch.observations[:1])
    rec.write(batch2)
    got = list(read_batches(tmp_path, batch.fetched_at - 1, batch.fetched_at + 60))
    assert [len(o) for _, o in got] == [4, 1]
    assert got[0][1][0] == decode(encode(batch.observations[0]))
    assert rec.prune(batch.fetched_at + 30 * 86400) == 1
