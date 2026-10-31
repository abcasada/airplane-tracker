"""A generic "looks like it's arriving at this airport" score. No airport-specific logic.

Combines heading toward the field (or lined up with a runway), descent, a believable height for
the distance (about a 3° path plus pattern margin), proximity and speed. 0 = no sign, 1 = on
final. Users decide what threshold matters in their filters.
"""

import math
from dataclasses import dataclass

from app import geo

MAX_DIST_NM = 40.0
MAX_HEIGHT_FT = 12000.0
FT_PER_NM_3DEG = 318.0


@dataclass(frozen=True, slots=True)
class Runway:
    ident: str
    lat: float
    lon: float
    heading: float  # landing direction, degrees true


@dataclass(frozen=True, slots=True)
class ArrivalInputs:
    lat: float
    lon: float
    height_ft: float  # above field elevation
    gs_kt: float | None
    track_deg: float | None
    vrate_fpm: float | None
    on_ground: bool


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def aligned_runway(a: ArrivalInputs, runways: list[Runway]) -> tuple[str, float] | None:
    """The runway the aircraft is lined up for (within 15 nm of the threshold), with distance."""
    if a.track_deg is None:
        return None
    best: tuple[str, float] | None = None
    for r in runways:
        d = geo.haversine_nm(r.lat, r.lon, a.lat, a.lon)
        if d > 15 or abs(geo.angle_diff(a.track_deg, r.heading)) > 20:
            continue
        # The approach lies opposite the landing direction from the threshold.
        approach_bearing = (r.heading + 180) % 360
        off = abs(geo.angle_diff(geo.bearing_deg(r.lat, r.lon, a.lat, a.lon), approach_bearing))
        lateral = d * abs(math.sin(math.radians(off)))
        along = d * math.cos(math.radians(off))
        if along > 0 and lateral <= max(0.6, along * 0.1 + 0.3) and (best is None or d < best[1]):
            best = (r.ident, d)
    return best


def arrival_score(
    a: ArrivalInputs, field_lat: float, field_lon: float, runways: list[Runway]
) -> tuple[float, str | None]:
    """Returns (score 0–1, runway ident if lined up)."""
    if a.on_ground:
        return 0.0, None
    d = geo.haversine_nm(field_lat, field_lon, a.lat, a.lon)
    if d > MAX_DIST_NM or a.height_ft > MAX_HEIGHT_FT:
        return 0.0, None

    rwy = aligned_runway(a, runways)
    if a.track_deg is None:
        s_dir = 0.3
    else:
        to_field = geo.bearing_deg(a.lat, a.lon, field_lat, field_lon)
        off = abs(geo.angle_diff(a.track_deg, to_field))
        # Close in, aircraft maneuver around the field (pattern legs), so be generous.
        tolerance = 60.0 if d > 5 else 120.0
        s_dir = _clamp(1 - off / tolerance)
    if rwy:
        s_dir = 1.0

    v = a.vrate_fpm
    if v is None:
        s_desc = 0.5
    elif v <= -300:
        s_desc = 1.0
    elif v < 300:
        s_desc = 0.7 if a.height_ft < 6000 else 0.35
    else:
        s_desc = 0.0 if v > 800 else 0.25

    ceiling = d * FT_PER_NM_3DEG * 1.5 + 2000
    s_height = _clamp(1 - (a.height_ft - ceiling) / 5000) if a.height_ft > ceiling else 1.0
    s_dist = _clamp(1 - d / MAX_DIST_NM)
    gs = a.gs_kt
    s_speed = 1.0 if gs is None or gs <= 250 else _clamp(1 - (gs - 250) / 200)

    score = s_dir * (0.3 + 0.7 * s_desc) * s_height * (0.6 + 0.4 * s_dist) * (0.5 + 0.5 * s_speed)
    return round(_clamp(score), 3), rwy[0] if rwy else None


def runways_for(rows: list[dict[str, float | str | None]]) -> list[Runway]:
    """Both landing directions for each runway row from OurAirports."""
    out: list[Runway] = []
    for r in rows:
        for end in ("le", "he"):
            lat, lon, hdg = r.get(f"{end}_lat"), r.get(f"{end}_lon"), r.get(f"{end}_heading")
            if isinstance(lat, float) and isinstance(lon, float) and isinstance(hdg, float):
                out.append(Runway(str(r.get(f"{end}_ident") or "?"), lat, lon, hdg))
    return out
