"""Opt-in live sources: FlyItalyADSB, OpenSky, the local receiver. All HTTP is mocked."""

import math
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.clock import FakeClock
from app.filters.facts import LiveFacts
from app.ingest.planner import QueryCircle
from app.ingest.poller import Poller
from app.ingest.recorder import read_batches
from app.main import create_app
from app.sources.base import AreaQuery, FeedSource, SourceBatch, SourceError
from app.sources.opensky import OpenSkySource, bbox, credits_for, parse_states
from app.sources.readsb import flyitaly
from app.sources.receiver import ReceiverSource
from app.sources.registry import build_sources
from tests.conftest import make_settings

T = 1_800_000_000.0


def flyitaly_payload(now: float) -> dict[str, Any]:
    """readsb/ADS-B Exchange v2 shape as FlyItalyADSB documents it ("now" in seconds); values
    are synthetic."""
    return {
        "now": now, "count": 1,
        "ac": [{"hex": "ae5420", "type": "adsb_icao", "flight": "RCH452 ", "r": "05-5140",
                "t": "C17", "dbFlags": 1, "alt_baro": 9000, "gs": 300.0, "track": 90.0,
                "lat": 38.9, "lon": -97.5, "seen_pos": 1.0, "seen": 0.5}],
    }  # fmt: skip


@respx.mock
async def test_flyitaly_source_uses_km_and_api_key() -> None:
    clock = FakeClock(T)
    async with httpx.AsyncClient() as http:
        src = flyitaly(http, clock, "test-key")
        assert src.max_rate_per_s == pytest.approx(0.112, abs=0.001)  # the monthly quota binds
        route = respx.get("https://api.flyitalyadsb.com/v2/lat/38.8/lon/-97.6/dist/65").mock(
            return_value=httpx.Response(200, json=flyitaly_payload(T))
        )
        batch = await src.fetch(AreaQuery(38.8, -97.6, 34.2))  # 35 nm → 65 km
        assert route.calls[0].request.headers["X-Api-Key"] == "test-key"
        assert "gzip" in route.calls[0].request.headers["Accept-Encoding"]
        o = batch.observations[0]
        assert (o.source, o.hex, o.callsign, o.registration) == (
            "flyitalyadsb", "ae5420", "RCH452", "05-5140",
        )  # fmt: skip
        assert o.pos_time == pytest.approx(T - 1.0)
        hexes = respx.get("https://api.flyitalyadsb.com/v2/icao/ae5420,ae5421").mock(
            return_value=httpx.Response(200, json=flyitaly_payload(T))
        )
        await src.fetch_hexes(["AE5420", "ae5421"])
        assert hexes.called
        respx.get("https://api.flyitalyadsb.com/v2/lat/38.8/lon/-97.6/dist/463").mock(
            return_value=httpx.Response(429)
        )
        with pytest.raises(SourceError) as e:
            await src.fetch(AreaQuery(38.8, -97.6, 400))  # capped at 250 nm
        assert e.value.is_client_error


async def test_keyed_sources_are_enabled_only_when_configured(tmp_path: Path) -> None:
    async with httpx.AsyncClient() as http:
        names = [s.name for s in build_sources(make_settings(tmp_path), http, FakeClock())]
        assert names == ["adsb.fi", "adsb.lol"]
        settings = make_settings(
            tmp_path, flyitaly_api_key="k", opensky_client_id="i", opensky_client_secret="s"
        )
        assert [s.name for s in build_sources(settings, http, FakeClock())] == [
            "adsb.fi", "adsb.lol", "flyitalyadsb", "opensky",
        ]  # fmt: skip
        only_id = make_settings(tmp_path, opensky_client_id="i")
        assert len(build_sources(only_id, http, FakeClock())) == 2
        with_rx = make_settings(tmp_path, receiver_url=RX_URL, receiver_uat_url=UAT_URL)
        rx = build_sources(with_rx, http, FakeClock())
        assert [s.name for s in rx[:2]] == ["receiver", "receiver-uat"]
        assert all(isinstance(s, FeedSource) for s in rx[:2])


# ---- Local receiver -----------------------------------------------------------------------------

RX_URL = "http://receiver.example/tar1090/data/aircraft.json"
UAT_URL = "http://receiver.example/skyaware978/data/aircraft.json"


def receiver_payload(now: float) -> dict[str, Any]:
    """readsb aircraft.json shape ("now" in seconds, "aircraft"); values are synthetic."""
    return {
        "now": now, "messages": 1000,
        "aircraft": [
            {"hex": "a1b2c3", "type": "adsb_icao", "flight": "N123AB  ", "alt_baro": 2500,
             "gs": 95.0, "track": 170.0, "lat": 38.85, "lon": -97.6, "seen_pos": 0.4, "seen": 0.1,
             "category": "A1"},
            {"hex": "a00001", "seen": 3.0, "rssi": -30.1},  # Mode S only, no position
        ],
    }  # fmt: skip


@respx.mock
async def test_receiver_uses_our_clock_and_marks_uat_as_adsb() -> None:
    clock = FakeClock(T)
    async with httpx.AsyncClient() as http:
        rx = ReceiverSource("receiver", RX_URL, http, clock)
        respx.get(RX_URL).mock(return_value=httpx.Response(200, json=receiver_payload(T - 90)))
        batch = await rx.fetch_all()  # the receiver's clock is 90 s slow: ignored
        o = next(o for o in batch.observations if o.hex == "a1b2c3")
        assert o.pos_time == pytest.approx(T - 0.4) and o.seen_at == pytest.approx(T - 0.1)
        assert batch.fetched_at == T and len(batch.observations) == 2
        uat_payload = {
            "now": T,
            "aircraft": [
                {
                    "hex": "~2a0001",
                    "lat": 38.8,
                    "lon": -97.6,
                    "alt_baro": 1500,
                    "seen_pos": 1.0,
                    "seen": 1.0,
                }
            ],
        }
        respx.get(UAT_URL).mock(return_value=httpx.Response(200, json=uat_payload))
        uat = await ReceiverSource("receiver-uat", UAT_URL, http, clock).fetch_all()
        assert uat.observations[0].pos_source == "adsb"
        respx.get(RX_URL).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(SourceError) as e:
            await rx.fetch_all()
        assert not e.value.is_client_error


@respx.mock
async def test_receiver_feeds_the_merge_every_second(tmp_path: Path) -> None:
    clock = FakeClock(T)
    app = create_app(make_settings(tmp_path, receiver_url=RX_URL), clock)
    route = respx.get(RX_URL).mock(
        side_effect=lambda _req: httpx.Response(200, json=receiver_payload(clock.now()))
    )
    async with app.router.lifespan_context(app):
        rt = app.state.ctx.rt()
        assert await rt.poller.tick() == pytest.approx(1.0)  # no places: only the receiver
        track = rt.store.get("a1b2c3")
        assert track is not None and track.sources(clock.now()) == ["receiver"]
        facts = LiveFacts(track, rt.eval_context())
        assert facts.get("seen_by_my_receiver") is True
        assert facts.get("position_source") == "adsb"
        for _ in range(5):
            clock.advance(1)
            await rt.poller.tick()
        assert route.call_count == 6
        st = next(s for s in rt.poller.status() if s["name"] == "receiver")
        assert st["kind"] == "receiver" and st["healthy"] and st["last_aircraft"] == 2
        # Read every second, recorded every 5 s.
        rec = list(read_batches(tmp_path / "recordings", T - 1, T + 10))
        assert [b for b, _ in rec] == [T, T + 5]


# ---- OpenSky ------------------------------------------------------------------------------------

STATES_URL = "https://opensky-network.org/api/states/all"
TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
)


def state(hex_: str, **kw: Any) -> list[Any]:
    """A state vector in OpenSky's documented field order (extended=1); values are synthetic."""
    sv: list[Any] = [hex_, "RCH452  ", "United States", T - 2, T - 1, -97.5, 38.9, 3000.0, False,
                     150.0, 90.0, -5.0, None, 3100.0, "4521", False, 0, 6]  # fmt: skip
    for i, v in kw.items():
        sv[int(i[1:])] = v
    return sv


def test_opensky_bbox_and_credits() -> None:
    near = AreaQuery(38.8, -97.6, 35)
    lamin, lomin, lamax, lomax = bbox(near)
    assert lamax - lamin == pytest.approx(70 / 60, abs=1e-3)
    assert lomax - lomin == pytest.approx(70 / 60 / math.cos(math.radians(38.8)), abs=1e-3)
    assert credits_for(near) == 1
    assert credits_for(AreaQuery(38.8, -97.6, 85)) == 1  # ~10 square degrees
    assert credits_for(AreaQuery(38.8, -97.6, 250)) == 2  # ~89 square degrees


def test_opensky_parse_converts_units() -> None:
    batch = parse_states(
        {"time": int(T), "states": [state("AE5420"), state("a1b2c3", s3=T - 300, s8=True, s7=None),
                                     ["bad"]]},
        T,
    )  # fmt: skip
    o, ground = batch.observations
    assert (o.hex, o.source, o.callsign, o.squawk, o.category) == (
        "ae5420", "opensky", "RCH452", "4521", "A5",
    )  # fmt: skip
    assert o.alt_baro_ft == pytest.approx(9843, abs=1)
    assert o.gs_kt == pytest.approx(291.6, abs=0.1)
    assert o.baro_rate_fpm == pytest.approx(-984, abs=1)
    assert o.pos_time == T - 2 and o.seen_at == T - 1 and o.pos_source == "adsb"
    assert ground.on_ground and ground.lat is None  # position 5 minutes old: dropped


@respx.mock
async def test_opensky_oauth_token_is_cached_and_renewed() -> None:
    clock = FakeClock(T)
    token = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": f"tok{i}", "expires_in": 1800})
            for i in range(3)
        ]
    )
    states = respx.get(STATES_URL).mock(
        return_value=httpx.Response(
            200, json={"time": int(T), "states": [state("ae5420")]},
            headers={"X-Rate-Limit-Remaining": "3999"},
        )
    )  # fmt: skip
    async with httpx.AsyncClient() as http:
        src = OpenSkySource(http, clock, "my-client", "my-secret")
        await src.fetch(AreaQuery(38.8, -97.6, 35))
        await src.fetch(AreaQuery(38.8, -97.6, 35))
        assert token.call_count == 1
        form = token.calls[0].request.content.decode()
        assert "grant_type=client_credentials" in form and "client_id=my-client" in form
        req = states.calls[0].request
        assert req.headers["Authorization"] == "Bearer tok0"
        assert req.url.params["lamin"] == "38.2167" and req.url.params["extended"] == "1"
        assert src.status_extra() == {"credits_remaining": 3999}
        clock.advance(1800 - 30)  # within a minute of expiry: renew first
        await src.fetch(AreaQuery(38.8, -97.6, 35))
        assert token.call_count == 2
        # A 401 means the token is no longer valid: renew once and retry.
        states.side_effect = [
            httpx.Response(401),
            httpx.Response(200, json={"time": int(T), "states": []}),
        ]
        batch = await src.fetch(AreaQuery(38.8, -97.6, 35))
        assert token.call_count == 3 and batch.observations == []
        assert "my-secret" not in str(states.calls[-1].request.url)


@respx.mock
async def test_opensky_out_of_credits_backs_off_for_as_long_as_it_says() -> None:
    clock = FakeClock(T)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 1800})
    )
    respx.get(STATES_URL).mock(
        return_value=httpx.Response(429, headers={"X-Rate-Limit-Retry-After-Seconds": "7200"})
    )

    async def on_batch(_b: SourceBatch, _c: QueryCircle) -> None:
        return None

    async with httpx.AsyncClient() as http:
        src = OpenSkySource(http, clock, "id", "secret", daily_credits=8000)
        circle = QueryCircle("inner:x", 38.8, -97.6, 35, 10, "inner")
        poller = Poller([src], clock, lambda: [circle], on_batch)
        await poller.tick()
        st = poller.status()[0]
        assert st["budget_unit"] == "credit" and st["budget_per_day"] == pytest.approx(4000)
        assert st["healthy"] is False and st["open_for_s"] == pytest.approx(7200)
        assert st["used_today"] == 1
