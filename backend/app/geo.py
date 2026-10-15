"""Geometry in aviation units: nautical miles, feet, degrees true."""

import math
from collections.abc import Sequence

EARTH_RADIUS_NM = 3440.065
FT_PER_NM = 6076.12


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def destination(lat: float, lon: float, bearing: float, dist_nm: float) -> tuple[float, float]:
    d = dist_nm / EARTH_RADIUS_NM
    b = math.radians(bearing)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2)
    )
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def to_local(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """Equirectangular projection around (lat0, lon0): (east_nm, north_nm).

    Error stays well under 1% within 250 nm, far below prediction uncertainty.
    """
    x = math.radians(lon - lon0) * math.cos(math.radians((lat + lat0) / 2)) * EARTH_RADIUS_NM
    y = math.radians(lat - lat0) * EARTH_RADIUS_NM
    return x, y


def from_local(x: float, y: float, lat0: float, lon0: float) -> tuple[float, float]:
    lat = lat0 + math.degrees(y / EARTH_RADIUS_NM)
    lon = lon0 + math.degrees(x / (EARTH_RADIUS_NM * math.cos(math.radians((lat + lat0) / 2))))
    return lat, lon


def elevation_angle_deg(ground_dist_nm: float, height_above_observer_ft: float) -> float:
    """Angle above the horizon, ignoring curvature and refraction (fine within ~30 nm)."""
    return math.degrees(math.atan2(height_above_observer_ft, ground_dist_nm * FT_PER_NM))


def slant_range_nm(ground_dist_nm: float, height_above_observer_ft: float) -> float:
    return math.hypot(ground_dist_nm, height_above_observer_ft / FT_PER_NM)


def point_in_polygon(lat: float, lon: float, polygon: Sequence[tuple[float, float]]) -> bool:
    """Ray casting on (lat, lon) vertices. Fine for small, non-polar polygons."""
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        yi, xi = polygon[i]
        yj, xj = polygon[j]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def angle_diff(a: float, b: float) -> float:
    """Smallest signed difference a - b in degrees, in [-180, 180)."""
    return (a - b + 180.0) % 360.0 - 180.0


def cross_and_along_track_nm(
    lat: float, lon: float, lat1: float, lon1: float, lat2: float, lon2: float
) -> tuple[float, float]:
    """Distance off the great circle from point 1 to point 2 (signed, nm), and how far along it."""
    d13 = haversine_nm(lat1, lon1, lat, lon) / EARTH_RADIUS_NM
    t13 = math.radians(bearing_deg(lat1, lon1, lat, lon))
    t12 = math.radians(bearing_deg(lat1, lon1, lat2, lon2))
    xt = math.asin(max(-1.0, min(1.0, math.sin(d13) * math.sin(t13 - t12))))
    cos_xt = math.cos(xt)
    at = math.acos(max(-1.0, min(1.0, math.cos(d13) / cos_xt))) if cos_xt else 0.0
    if math.cos(t13 - t12) < 0:
        at = -at
    return xt * EARTH_RADIUS_NM, at * EARTH_RADIUS_NM
