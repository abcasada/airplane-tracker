"""Synthetic flights for prediction scenarios, fed through the real track pipeline."""

from __future__ import annotations

from dataclasses import dataclass

from app import geo
from app.sources.base import Observation
from app.tracks.store import Track, TrackStore

T0 = 1_800_000_000.0


@dataclass
class FlightState:
    lat: float
    lon: float
    alt_ft: float
    gs_kt: float
    track_deg: float
    vrate_fpm: float = 0.0
    turn_dps: float = 0.0
    nav_heading: float | None = None
    nav_alt: float | None = None
    category: str = "A3"


def fly(
    start: FlightState, seconds: float, step_s: float = 10.0, hex_: str = "abc123"
) -> tuple[TrackStore, Track, float]:
    """Fly for `seconds`, reporting every `step_s`. Returns the store, track and end time."""
    store = TrackStore()
    s = start
    t = T0
    track: Track | None = None
    steps = int(seconds // step_s)
    for i in range(steps + 1):
        obs = Observation(
            hex=hex_, source="test", seen_at=t, pos_time=t, lat=s.lat, lon=s.lon,
            alt_baro_ft=s.alt_ft, gs_kt=s.gs_kt, track_deg=s.track_deg % 360,
            baro_rate_fpm=s.vrate_fpm, nav_heading_deg=s.nav_heading,
            nav_altitude_ft=s.nav_alt, category=s.category, pos_source="adsb",
        )  # fmt: skip
        track, _ = store.update(obs)
        if i == steps:
            break
        # Advance with the mid-step heading for accuracy in turns.
        mid = s.track_deg + s.turn_dps * step_s / 2
        dist = s.gs_kt * step_s / 3600.0
        s.lat, s.lon = geo.destination(s.lat, s.lon, mid % 360, dist)
        s.track_deg = (s.track_deg + s.turn_dps * step_s) % 360
        s.alt_ft += s.vrate_fpm * step_s / 60.0
        t += step_s
    assert track is not None
    return store, track, t


def at(place_lat: float, place_lon: float, bearing: float, dist_nm: float) -> tuple[float, float]:
    return geo.destination(place_lat, place_lon, bearing, dist_nm)
