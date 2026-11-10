"""Opt-in live sources: FlyItalyADSB, OpenSky, the local receiver. All HTTP is mocked."""

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.clock import FakeClock
from app.sources.base import AreaQuery, SourceError
from app.sources.readsb import flyitaly
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
        settings = make_settings(tmp_path, flyitaly_api_key="k")
        assert [s.name for s in build_sources(settings, http, FakeClock())] == [
            "adsb.fi", "adsb.lol", "flyitalyadsb",
        ]  # fmt: skip
