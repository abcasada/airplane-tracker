"""Prediction scenarios. Expected outcomes are the spec: don't loosen them to make a change pass."""

from collections import deque
from dataclasses import replace
from typing import Any

import pytest

from app.filters.facts import ANY_VIEWPOINT, EvalContext, LiveFacts
from app.places.models import Place, PredictionOverrides, Visibility
from app.predict.arrival import runways_for
from app.predict.engine import PredictionSettings, Predictor, is_active, prediction_for
from app.tracks.store import Track, TrackPoint
from tests.scenarios.flight import FlightState, at, fly

HOME = Place(id=1, name="Home", lat=38.84, lon=-97.61, elevation_ft=1220, radius_nm=10,
             visibility=Visibility(), is_home=True, created_at=0, updated_at=0)  # fmt: skip
RANGE = Place(
    id=2, name="Range", role="area", lat=38.67, lon=-97.85, radius_nm=10, created_at=0,
    updated_at=0, alt_ceiling_ft=23000,
    polygon=[(38.7556, -97.7669), (38.6625, -97.7669), (38.6389, -97.7919), (38.6389, -97.8336),
             (38.5833, -97.8336), (38.5833, -97.9336), (38.7556, -97.9336)],
)  # fmt: skip
KSLN = Place(id=3, name="KSLN", role="airport", airport_icao="KSLN", lat=38.791, lon=-97.652,
             elevation_ft=1288, radius_nm=15, created_at=0, updated_at=0)  # fmt: skip
RUNWAYS = runways_for(
    [{"le_ident": "17", "le_lat": 38.8075, "le_lon": -97.6522, "le_heading": 180.0,
      "he_ident": "35", "he_lat": 38.7738, "he_lon": -97.6522, "he_heading": 360.0}]
)  # fmt: skip
PLACES = [HOME, RANGE, KSLN]


def run(
    start: FlightState, seconds: float = 120, extra_updates: int = 2
) -> tuple[Track, Predictor]:
    """Fly, then run the predictor on the last few reports (hysteresis needs ≥2)."""
    _store, track, end = fly(start, seconds)
    predictor = Predictor(PredictionSettings(), seed=1)
    for i in range(extra_updates, -1, -1):
        # Re-evaluate the same state a few times, as successive poll cycles would.
        predictor.update([track], PLACES, end - i * 0.1, {3: RUNWAYS})
    return track, predictor


def test_straight_overflight_predicts_visible_with_eta() -> None:
    lat, lon = at(HOME.lat, HOME.lon, 180, 25)
    track, _ = run(FlightState(lat, lon, 8000, 250, 0))
    pred = prediction_for(track, HOME.id)
    assert pred is not None
    assert is_active(track, HOME.id)
    # 2 min flown ≈ 8.3 nm, ~16.7 nm left; visible inside ~4.2 nm at 15° → ≈ 180 s.
    assert pred.eta_s == pytest.approx(180, abs=30)
    assert pred.confidence >= 0.6
    # High overhead pass; the ensemble's lateral spread (~0.5 nm after 4 min) keeps the median
    # below the 70°+ of a perfectly straight line.
    assert pred.max_elevation_deg is not None and pred.max_elevation_deg > 45


def test_passing_six_miles_east_is_never_high_enough() -> None:
    lat, lon = at(HOME.lat, HOME.lon, 160, 20)  # parallel track ~6.8 nm east
    lat, lon = at(lat, lon, 90, 0)
    track, _ = run(FlightState(*at(HOME.lat, HOME.lon, 90, 6.8), 8000, 250, 0), seconds=10)
    assert not is_active(track, HOME.id)
    pred = prediction_for(track, HOME.id)
    assert pred is None or pred.confidence < 0.3


def test_aircraft_turning_away_is_not_predicted() -> None:
    lat, lon = at(HOME.lat, HOME.lon, 180, 22)
    # Heading at home but in a steady right turn that has already swung it 60° off.
    track, _ = run(FlightState(lat, lon, 8000, 250, 300, turn_dps=1.0), seconds=60)
    assert not is_active(track, HOME.id)


def test_orbiting_aircraft_nearby_does_not_trigger() -> None:
    lat, lon = at(HOME.lat, HOME.lon, 270, 14)
    track, _ = run(FlightState(lat, lon, 6000, 140, 0, turn_dps=3.0, category="A1"), seconds=240)
    assert not is_active(track, HOME.id)


def test_autopilot_heading_turn_toward_home_is_predicted() -> None:
    # 12 nm south, flying east with heading north selected; after 60 s of history it's about
    # 4 nm west of home's meridian and about to turn.
    lat, lon = at(*at(HOME.lat, HOME.lon, 180, 12), 270, 8)
    track, _ = run(FlightState(lat, lon, 9000, 220, 90, nav_heading=20.0), seconds=60)
    assert is_active(track, HOME.id)
    pred = prediction_for(track, HOME.id)
    assert pred is not None and pred.eta_s is not None and pred.eta_s < 300


def test_area_entry_with_altitude_band() -> None:
    lat, lon = at(RANGE.lat, RANGE.lon, 90, 22)
    track, _ = run(FlightState(lat, lon, 15000, 300, 270), seconds=30)
    assert is_active(track, RANGE.id)
    high, _ = run(FlightState(lat, lon, 30000, 300, 270), seconds=30)
    assert not is_active(high, RANGE.id)  # above the area's ceiling


def test_already_visible_is_active_at_once() -> None:
    lat, lon = at(HOME.lat, HOME.lon, 0, 0.5)
    _store, track, end = fly(FlightState(lat, lon, 5000, 200, 90), 20)
    predictor = Predictor(PredictionSettings(), seed=1)
    predictor.update([track], [HOME], end, {})
    assert is_active(track, HOME.id)
    assert prediction_for(track, HOME.id).eta_s == 0  # type: ignore[union-attr]


def test_final_approach_to_ksln_is_predicted_arrival() -> None:
    lat, lon = at(KSLN.lat, KSLN.lon, 180, 9)
    track, _ = run(FlightState(lat, lon, 4000, 160, 0, vrate_fpm=-700), seconds=30)
    assert is_active(track, KSLN.id)
    pred = prediction_for(track, KSLN.id)
    assert pred is not None and pred.eta_s is not None and 60 < pred.eta_s < 240


def test_single_confident_update_does_not_turn_on() -> None:
    lat, lon = at(HOME.lat, HOME.lon, 180, 25)
    _store, track, end = fly(FlightState(lat, lon, 8000, 250, 0), 120)
    predictor = Predictor(PredictionSettings(), seed=1)
    predictor.update([track], [HOME], end, {})
    assert not is_active(track, HOME.id)
    predictor.update([track], [HOME], end + 1, {})
    assert is_active(track, HOME.id)


def _track_until(track: Track, pts: list[TrackPoint], extra: dict[str, Any]) -> Track:
    """The track as it looked at the last of `pts`, sharing prediction state via `extra`."""
    last = pts[-1]
    sub = replace(track, history=deque(pts, maxlen=track.history.maxlen), extra=extra)
    sub.lat, sub.lon, sub.pos_time, sub.last_seen = last.lat, last.lon, last.t, last.t
    sub.alt_baro_ft, sub.gs_kt, sub.track_deg = last.alt_ft, last.gs_kt, last.track_deg
    return sub


def _facts(track: Track) -> LiveFacts:
    return LiveFacts(track, EvalContext(track.pos_time or 0.0, {p.id: p for p in PLACES}))


def test_prediction_fields_expose_the_prediction() -> None:
    lat, lon = at(HOME.lat, HOME.lon, 180, 25)
    track, _ = run(FlightState(lat, lon, 8000, 250, 0))
    f = _facts(track)
    pred = prediction_for(track, HOME.id)
    assert pred is not None
    assert f.get("predicted_visible", HOME.id) is True
    assert f.get("visible_eta_s", HOME.id) == pred.eta_s
    assert f.get("visible_confidence", HOME.id) == pred.confidence
    assert f.get("predicted_max_elevation_deg", HOME.id) == pred.max_elevation_deg
    assert f.get("predicted_closest_nm", HOME.id) == pytest.approx(pred.cpa_nm)
    # Any active viewpoint: booleans take "any", ETAs the soonest, confidence the highest.
    assert f.get("predicted_visible", ANY_VIEWPOINT) is True
    assert f.get("visible_eta_s", ANY_VIEWPOINT) == pred.eta_s
    assert f.get("visible_confidence", ANY_VIEWPOINT) == pred.confidence
    # Northbound away from the range and KSLN: no entry or arrival.
    assert f.get("predicted_entry", RANGE.id) is False
    assert f.get("predicted_arrival", KSLN.id) is False


def test_entry_and_arrival_fields() -> None:
    lat, lon = at(RANGE.lat, RANGE.lon, 90, 22)
    track, _ = run(FlightState(lat, lon, 15000, 300, 270), seconds=30)
    f = _facts(track)
    assert f.get("predicted_entry", RANGE.id) is True
    eta = f.get("entry_eta_s", RANGE.id)
    assert eta is not None and 60 < eta < 300
    assert f.get("entry_confidence", RANGE.id) >= 0.6

    lat, lon = at(KSLN.lat, KSLN.lon, 180, 9)
    track, _ = run(FlightState(lat, lon, 4000, 160, 0, vrate_fpm=-700), seconds=30)
    f = _facts(track)
    assert f.get("predicted_arrival", KSLN.id) is True
    eta = f.get("arrival_eta_s", KSLN.id)
    assert eta is not None and 60 < eta < 240


def test_place_override_raises_the_bar() -> None:
    strict = HOME.model_copy(update={"prediction": PredictionOverrides(min_confidence=0.99)})
    lat, lon = at(HOME.lat, HOME.lon, 180, 25)
    _store, track, end = fly(FlightState(lat, lon, 8000, 250, 0), 120)
    predictor = Predictor(PredictionSettings(), seed=1)
    for i in range(3, -1, -1):
        predictor.update([track], [strict], end - i * 0.1, {})
    assert not is_active(track, HOME.id)


def test_circling_near_the_airport_is_not_predicted_arrival() -> None:
    # Steady 3°/s orbit 3 nm from KSLN at ~1,700 ft above the field: each lap briefly points at
    # the field, but it isn't arriving.
    lat, lon = at(KSLN.lat, KSLN.lon, 300, 3)
    for start_hdg in (0, 90, 180, 270):
        _store, track, end = fly(
            FlightState(lat, lon, 3000, 100, start_hdg, turn_dps=3.0, category="A1"), 600
        )
        predictor = Predictor(PredictionSettings(), seed=1)
        on = 0
        extra: dict[str, Any] = {}
        # Re-run the last few minutes cycle by cycle, as the live loop would.
        for i in range(30):
            t = end - (29 - i) * 10
            pts = [p for p in track.history if p.t <= t]
            sub = _track_until(track, pts, extra)
            predictor.update([sub], [KSLN], t, {3: RUNWAYS})
            on += is_active(sub, KSLN.id)
        assert on == 0, f"orbit starting {start_hdg}° predicted arrival on {on} cycles"


def test_turn_to_final_is_predicted_once_rolled_out() -> None:
    # Base-to-final turn onto runway 35 that rolled out 6 nm south of the field.
    lat, lon = at(KSLN.lat, KSLN.lon, 180, 6)
    track, _ = run(FlightState(lat, lon, 3200, 130, 0, vrate_fpm=-600), seconds=40)
    assert is_active(track, KSLN.id)
