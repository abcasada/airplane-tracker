"""Flightradar24 (paid): parsing, credit cap, cache, gap-fill planning. All HTTP is mocked and
every payload is synthetic."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.clock import FakeClock
from app.db import Database
from app.ingest.credits import CreditMeter, month_bounds, month_key
from app.ingest.fr24 import (
    GAP_FILL_INTERVAL_S,
    FR24Service,
    GapFiller,
    GapTarget,
    LookupRefusedError,
    matched_by,
)
from app.sources.fr24 import POSITIONS_URL, USAGE_URL, FR24Source, parse_positions

T = datetime(2026, 11, 15, 12, 0, tzinfo=UTC).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def flight(**kw: Any) -> dict[str, Any]:
    base = {
        "fr24_id": "3a1b2c3d", "flight": "RC452", "callsign": "RCH452", "lat": 38.9,
        "lon": -97.5, "track": 90, "alt": 9000, "gspeed": 300, "vspeed": -640,
        "squawk": "4521", "timestamp": iso(T - 5), "source": "ADSB", "hex": "AE5420",
        "type": "C17", "reg": "05-5140", "painted_as": None, "operating_as": None,
        "orig_iata": None, "orig_icao": "KDOV", "dest_iata": None, "dest_icao": "KSLN",
        "eta": iso(T + 1800),
    }  # fmt: skip
    base.update(kw)
    return base


@pytest.fixture
async def db(tmp_path: Path) -> Any:
    d = Database(tmp_path / "t.sqlite3")
    await d.connect()
    await d.migrate()
    yield d
    await d.close()


def test_parse_full_positions() -> None:
    payload = {
        "data": [
            flight(),
            flight(fr24_id="x2", hex=None, source="ESTIMATED", squawk="0"),
            {"fr24_id": "bad", "lat": "x"},
        ]
    }
    fl = parse_positions(payload)
    assert len(fl) == 2
    f = fl[0]
    assert (f.hex, f.registration, f.callsign, f.type_code) == (
        "ae5420",
        "05-5140",
        "RCH452",
        "C17",
    )
    assert (f.orig_icao, f.dest_icao) == ("KDOV", "KSLN")
    assert f.eta == pytest.approx(T + 1800)
    o = f.to_observation(T)
    assert o is not None
    assert (o.source, o.alt_baro_ft, o.gs_kt, o.baro_rate_fpm, o.pos_source) == (
        "fr24", 9000.0, 300.0, -640.0, "adsb",
    )  # fmt: skip
    assert o.pos_time == pytest.approx(T - 5)
    # Estimated positions (and flights without a hex) are shown, never fed into tracks.
    assert fl[1].estimated and fl[1].to_observation(T) is None and fl[1].squawk is None
    ground = parse_positions({"data": [flight(alt=0, gspeed=12)]})[0].to_observation(T)
    assert ground is not None and ground.on_ground and ground.alt_baro_ft is None


def test_credit_meter_cap_pace_and_month_rollover() -> None:
    m = CreditMeter(cap=3000)
    start, end = month_bounds(T)
    assert month_key(T) == "2026-11" and end - start == 30 * 86400
    early = start + 3600
    assert m.pace_limit(early) == pytest.approx(3000 * (3600 + 86400) / (30 * 86400))
    assert m.refusal(early, 50, paced=True) is None
    m.spend(early, 120)
    assert m.refusal(early, 8, paced=True) is not None  # ahead of pace: background waits
    assert m.refusal(early, 8, paced=False) is None  # the user's own lookup still goes
    m.spend(early, 2870)
    assert "cap" in (m.refusal(early, 20, paced=False) or "")
    m.mark_exhausted(early)
    assert "used up" in (m.refusal(early, 1, paced=False) or "")
    restored = CreditMeter(cap=3000)
    restored.load(m.to_json())
    assert restored.used == 2990 and restored.exhausted
    assert restored.refusal(end + 1, 8, paced=False) is None  # a new month starts from zero
    assert restored.used == 0 and not restored.exhausted


@respx.mock
async def test_lookup_charges_credits_caches_and_enforces_cap(db: Database) -> None:
    clock = FakeClock(T)
    async with httpx.AsyncClient() as http:
        svc = FR24Service(FR24Source(http, clock, "test-key"), db, clock, 20, 8.0)
        await svc.load()
        route = respx.get(POSITIONS_URL).mock(
            return_value=httpx.Response(200, json={"data": [flight()]})
        )
        r = await svc.lookup(["05-5140"], ["RCH452"], "on_demand")
        req = route.calls[0].request
        assert req.headers["Authorization"] == "Bearer test-key"
        assert req.headers["Accept-Version"] == "v1"
        assert req.url.params["registrations"] == "05-5140"
        assert req.url.params["callsigns"] == "RCH452"
        assert req.url.params["limit"] == "2"
        assert r.credits == 8.0 and not r.from_cache
        assert [o.hex for o in r.batch.observations] == ["ae5420"]
        assert svc.meter.used == 8.0
        # Persisted, so a restart keeps counting the same month.
        assert (await db.get_json("fr24_credits"))["used"] == 8.0
        # Served from the cache: no request, no credits.
        clock.advance(30)
        r2 = await svc.lookup(["05-5140"], [], "on_demand")
        assert r2.from_cache and r2.credits == 0 and route.call_count == 1
        # Cap 20: another 2-identifier lookup could cost 16 > 12 left, so it's refused unsent.
        clock.advance(120)
        with pytest.raises(LookupRefusedError, match="cap"):
            await svc.lookup(["N12345"], ["ABC123"], "on_demand")
        assert route.call_count == 1
        # A miss is charged one flight's worth and cached as a miss.
        route.mock(return_value=httpx.Response(200, json={"data": []}))
        r3 = await svc.lookup(["N12345"], [], "on_demand")
        assert r3.flights == [] and svc.meter.used == 16.0
        clock.advance(60)
        assert (await svc.lookup(["N12345"], [], "on_demand")).from_cache


@respx.mock
async def test_lookup_rate_switch_breaker_and_402(db: Database) -> None:
    clock = FakeClock(T)
    async with httpx.AsyncClient() as http:
        svc = FR24Service(FR24Source(http, clock, "k"), db, clock, 20000, 8.0)
        route = respx.get(POSITIONS_URL).mock(
            return_value=httpx.Response(200, json={"data": [flight()]})
        )
        await svc.lookup(["05-5140"], [], "on_demand")
        # Half of FR24's ~10 req/min: one request per 12 s.
        with pytest.raises(LookupRefusedError, match="rate") as e:
            await svc.lookup(["N1"], [], "on_demand")
        assert e.value.retry_in_s == pytest.approx(12.0)
        clock.advance(12)
        svc.enabled = False
        with pytest.raises(LookupRefusedError, match="switched off"):
            await svc.lookup(["N1"], [], "on_demand")
        svc.enabled = True
        route.mock(return_value=httpx.Response(402, json={"message": "no credits"}))
        with pytest.raises(LookupRefusedError, match="402"):
            await svc.lookup(["N1"], [], "on_demand")
        assert svc.meter.exhausted and svc.breaker.is_open(clock.now())
        clock.advance(3600)
        with pytest.raises(LookupRefusedError, match="used up"):
            await svc.lookup(["N2"], [], "on_demand")
        assert route.call_count == 2
        st = svc.status()
        assert st["month"]["exhausted"] and st["errors"] == 1 and st["kind"] == "lookup"


@respx.mock
async def test_gap_fill_is_paced_and_can_be_off(db: Database) -> None:
    start, _ = month_bounds(T)
    clock = FakeClock(start + 3600)  # first hour of the month: pace allows ~1 day's share
    async with httpx.AsyncClient() as http:
        svc = FR24Service(FR24Source(http, clock, "k"), db, clock, 300, 8.0)
        respx.get(POSITIONS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
        await svc.lookup(["N1"], [], "gap_fill")  # 8 ≤ 300/30 = 10
        clock.advance(20)
        with pytest.raises(LookupRefusedError, match="pace"):
            await svc.lookup(["N2"], [], "gap_fill")
        await svc.lookup(["N2"], [], "on_demand")  # the user's own lookups aren't paced
        svc.gap_fill_enabled = False
        clock.advance(20)
        with pytest.raises(LookupRefusedError, match="gap-fill is off"):
            await svc.lookup(["N3"], [], "gap_fill")


@respx.mock
async def test_usage_report(db: Database) -> None:
    clock = FakeClock(T)
    async with httpx.AsyncClient() as http:
        svc = FR24Service(FR24Source(http, clock, "k"), db, clock, 20000, 8.0)
        route = respx.get(USAGE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "endpoint": "live/flight-positions/full",
                            "request_count": 3,
                            "credits": 24,
                        }
                    ]
                },
            )
        )
        rows = await svc.usage()
        assert rows == [{"endpoint": "live/flight-positions/full", "requests": 3, "credits": 24}]
        assert route.calls[0].request.url.params["period"] == "30d"
        await svc.usage()  # cached for 10 minutes
        assert route.call_count == 1


def test_gap_filler_schedule_and_batching() -> None:
    g = GapFiller()
    now = T
    a = GapTarget("watch:reg:N123AB", registration="N123AB")
    b = GapTarget("hex:ae5420", registration="05-5140", callsign="RCH452")
    c = GapTarget("hex:a00001", callsign="SKW1")
    nothing = GapTarget("hex:a00002")  # no registration or callsign: FR24 can't be asked
    dup = GapTarget("watch:hex:ae5420", registration="055140")  # same airframe as b
    due = g.due([a, b, c, nothing, dup], now)
    assert [t.key for t in due] == ["watch:reg:N123AB", "hex:ae5420", "hex:a00001"]
    g.done(a, True, now)
    g.done(b, False, now)
    assert [t.key for t in g.due([a, b, c], now + 60)] == ["hex:a00001"]
    assert {t.key for t in g.due([a, b, c], now + GAP_FILL_INTERVAL_S)} == {
        "watch:reg:N123AB", "hex:a00001",
    }  # fmt: skip
    assert "hex:ae5420" in {t.key for t in g.due([a, b, c], now + 2 * GAP_FILL_INTERVAL_S)}
    g.done(b, False, now + 600)  # still not found: the wait doubles
    assert g.state["hex:ae5420"][1] == 2 * 2 * GAP_FILL_INTERVAL_S
    g.due([a], now + 700)  # b heard again by a free source: forgotten
    assert "hex:ae5420" not in g.state
    fl = parse_positions({"data": [flight()]})
    assert matched_by(fl, b) and matched_by(fl, GapTarget("x", callsign="rch452"))
    assert not matched_by(fl, a)
