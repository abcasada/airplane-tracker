"""Sun position, day/night fields, sightings history and the history fields."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.filters.facts import EvalContext, LiveFacts
from app.history import SightingsBook, day_of
from app.sources.base import Observation, SourceBatch
from app.sun import sun_elevation_deg
from app.tracks.store import Track, TrackStore
from tests.conftest import Harness
from tests.test_filters import HOME, PLACES, RANGE

CHI = ZoneInfo("America/Chicago")


def ts(year: int, month: int, day: int, hour: int, minute: int) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp()


async def count(h: Harness) -> int:
    row = await h.ctx.db.fetchone("SELECT count(*) FROM sightings")
    assert row is not None
    return int(row[0])


def test_sun_elevation_known_values() -> None:
    # Solar noon on the June solstice at 37.69 N: 90 - 37.69 + 23.44.
    assert sun_elevation_deg(ts(2026, 6, 21, 18, 33), 37.69, -97.34) == pytest.approx(
        75.75, abs=0.2
    )
    # March equinox, Greenwich at noon: 90 - latitude.
    assert sun_elevation_deg(ts(2026, 3, 20, 12, 7), 51.48, 0.0) == pytest.approx(38.5, abs=0.3)
    # Kansas, local midnight in winter: far below the horizon.
    assert sun_elevation_deg(ts(2026, 12, 21, 6, 0), 38.84, -97.61) < -60
    # Sunset in Salina on 2026-06-21 is ~21:00 CDT (02:00 UTC next day).
    assert sun_elevation_deg(ts(2026, 6, 22, 1, 50), 38.84, -97.61) > -0.833
    assert sun_elevation_deg(ts(2026, 6, 22, 2, 15), 38.84, -97.61) < -0.833


def track(hex_: str = "ae1234", type_code: str = "C17", lat: float = 38.86, t: float = 0) -> Track:
    obs = Observation(hex=hex_, source="t", seen_at=t, pos_time=t, lat=lat, lon=-97.60,
                      alt_baro_ft=4220, type_code=type_code)  # fmt: skip
    return TrackStore().update(obs)[0]


def facts(tr: Track, now: float, book: SightingsBook | None) -> LiveFacts:
    return LiveFacts(tr, EvalContext(now, PLACES, CHI, {"sightings": book}))


def test_day_night_fields() -> None:
    noon = ts(2026, 6, 21, 18, 30)
    night = ts(2026, 6, 22, 5, 0)
    f = facts(track(), noon, None)
    assert f.get("daylight", HOME.id) is True and f.get("dark", HOME.id) is False
    assert f.get("sun_elevation_deg", HOME.id) > 70
    f = facts(track(), night, None)
    assert f.get("daylight", HOME.id) is False and f.get("dark", HOME.id) is True
    assert f.get("dark", 0) is True  # any active viewpoint
    assert f.get("daylight") is None  # needs a place


async def test_sightings_history_across_days(harness: Harness) -> None:
    db = harness.ctx.db
    book = SightingsBook(db)
    d1 = ts(2027, 1, 10, 18, 0)
    near, far = track(t=d1), track("a00001", "B738", lat=39.5, t=d1)
    other = track("a00002", "C17", t=d1)
    book.record([near, far], [HOME, RANGE], d1, CHI)
    book.record([near], [HOME], d1 + 600, CHI)
    await book.flush(d1 + 600)
    rows = await db.fetchall("SELECT * FROM sightings ORDER BY hex, place_id")
    # Only within the watch radius: home (10 nm) yes, the range's centre is ~13 nm away.
    assert [(r["hex"], r["place_id"], r["day"]) for r in rows] == [("ae1234", 1, "2027-01-10")]
    assert rows[0]["first_at"] == d1 and rows[0]["last_at"] == d1 + 600

    # A fresh book (restart) on a later day loads the history lazily.
    d4 = d1 + 3 * 86400
    later = SightingsBook(db)
    day4 = day_of(d4, CHI)
    assert later.visits_before("ae1234", HOME.id, day4) is None  # not loaded yet: unknown
    await later.prepare([near, other, far], day4)
    f = facts(near, d4, later)
    assert f.get("visits_prev_30d", HOME.id) == 1
    assert f.get("days_since_visit", HOME.id) == 3
    assert f.get("first_visit", HOME.id) is False
    assert f.get("type_visits_prev_30d", HOME.id) == 1
    g = facts(other, d4, later)  # another C-17: new airframe, known type
    assert g.get("first_visit", HOME.id) is True and g.get("days_since_visit", HOME.id) is None
    assert g.get("visits_prev_30d", HOME.id) == 0
    assert g.get("type_visits_prev_30d", HOME.id) == 1
    assert facts(far, d4, later).get("type_visits_prev_30d", HOME.id) == 0

    # Seen today: today doesn't count; tomorrow it does, without reloading the cache.
    later.record([near], [HOME], d4, CHI)
    await later.flush(d4)
    d5 = d4 + 86400
    f = facts(near, d5, later)
    assert f.get("visits_prev_30d", HOME.id) == 2 and f.get("days_since_visit", HOME.id) == 1
    assert f.get("type_visits_prev_30d", HOME.id) == 2
    # Outside the 30-day window.
    assert facts(near, d1 + 40 * 86400, later).get("visits_prev_30d", HOME.id) == 0

    # Retention and deleted places.
    await later.prune(d1 + 200 * 86400, CHI, {HOME.id})
    assert await count(harness) == 0


async def test_read_only_book_never_writes(harness: Harness) -> None:
    book = SightingsBook(harness.ctx.db, read_only=True)
    now = ts(2027, 1, 10, 18, 0)
    book.record([track(t=now)], [HOME], now, CHI)
    await book.flush(now)
    assert await count(harness) == 0


async def test_live_loop_records_sightings_and_reports_status(authed: Harness) -> None:
    c = authed.client
    home = {"name": "Home", "lat": 38.84, "lon": -97.61, "elevation_ft": 1220, "radius_nm": 10,
            "is_home": True}  # fmt: skip
    await c.post("/api/places", json=home)
    rt = authed.ctx.rt()
    now = authed.clock.now()
    obs = Observation(hex="ae1234", source="adsb.fi", seen_at=now, pos_time=now, lat=38.86,
                      lon=-97.60, alt_baro_ft=4220, type_code="C17")  # fmt: skip
    await rt.on_batch(SourceBatch("adsb.fi", now, [obs]), rt.circles()[0])
    await rt.sightings.flush(now)
    status = (await c.get("/api/status")).json()
    assert status["sightings"]["rows"] == 1 and status["sightings"]["days"] == 1
    detail = (await c.get("/api/aircraft/ae1234")).json()
    at_home = next(p["facts"] for p in detail["places"] if p["name"] == "Home")
    assert at_home["first_visit"] is True
    assert at_home["visits_prev_30d"] == 0
