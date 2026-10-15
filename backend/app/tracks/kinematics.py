"""Motion state derived from a track's recent history: smoothed track, turn rate, maneuvering."""

from __future__ import annotations

import itertools
import statistics
from dataclasses import dataclass

from app import geo

from .store import Track, TrackPoint

TURN_WINDOW_S = 60.0
VARIABILITY_WINDOW_S = 120.0


@dataclass(frozen=True, slots=True)
class Kinematics:
    track_deg: float
    gs_kt: float
    vrate_fpm: float
    turn_rate_dps: float  # + = right turn
    turn_variability_dps: float  # spread of turn rate over ~2 min: the "maneuvering" signal
    history_s: float


def _unwrap(tracks: list[float]) -> list[float]:
    out = [tracks[0]]
    for t in tracks[1:]:
        out.append(out[-1] + geo.angle_diff(t, out[-1] % 360.0))
    return out


def _slope(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den


def _track_points(history: list[TrackPoint]) -> list[tuple[float, float]]:
    """(time, track) pairs, using reported track or the course between positions."""
    out: list[tuple[float, float]] = []
    for prev, cur in itertools.pairwise(history):
        if cur.track_deg is not None:
            out.append((cur.t, cur.track_deg))
        elif geo.haversine_nm(prev.lat, prev.lon, cur.lat, cur.lon) > 0.05:
            out.append((cur.t, geo.bearing_deg(prev.lat, prev.lon, cur.lat, cur.lon)))
    return out


def derive(track: Track) -> Kinematics | None:
    """None when there isn't enough to project (no position, speed or direction)."""
    if track.lat is None or track.lon is None or track.on_ground:
        return None
    hist = list(track.history)
    now_t = hist[-1].t if hist else (track.pos_time or 0.0)
    gs = track.gs_kt
    trk = track.track_deg
    if (gs is None or trk is None) and len(hist) >= 2:
        a, b = hist[-2], hist[-1]
        dt = b.t - a.t
        if dt > 0:
            d = geo.haversine_nm(a.lat, a.lon, b.lat, b.lon)
            gs = gs if gs is not None else d / dt * 3600.0
            trk = trk if trk is not None else geo.bearing_deg(a.lat, a.lon, b.lat, b.lon)
    if gs is None or trk is None or gs < 30:
        return None

    pts = _track_points(hist)
    recent = [(t, v) for t, v in pts if now_t - t <= TURN_WINDOW_S]
    turn = 0.0
    if len(recent) >= 3:
        unwrapped = _unwrap([v for _, v in recent])
        turn = _slope([t for t, _ in recent], unwrapped)
    # Turn-rate variability: slopes over short windows across the last two minutes.
    window = [(t, v) for t, v in pts if now_t - t <= VARIABILITY_WINDOW_S]
    variability = 0.0
    if len(window) >= 5:
        unwrapped = _unwrap([v for _, v in window])
        times = [t for t, _ in window]
        rates = [
            (unwrapped[i + 1] - unwrapped[i]) / (times[i + 1] - times[i])
            for i in range(len(window) - 1)
            if times[i + 1] > times[i]
        ]
        if len(rates) >= 3:
            variability = statistics.pstdev(rates)
    vrate = track.vertical_rate_fpm or 0.0
    span = now_t - hist[0].t if hist else 0.0
    # Clamp implausible turn rates from noisy MLAT tracks.
    turn = max(-6.0, min(6.0, turn))
    return Kinematics(trk % 360.0, gs, vrate, turn, min(variability, 6.0), span)
