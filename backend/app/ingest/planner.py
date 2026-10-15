"""Turn active places into a few point/radius queries. Never asks a source for global data."""

import math
from dataclasses import dataclass

from app import geo
from app.places.models import Place

MAX_RADIUS_NM = 250.0
# Merge two circles when one enclosing circle is at most this much bigger than the larger one.
MERGE_SLACK = 1.15


@dataclass(frozen=True)
class QueryCircle:
    key: str
    lat: float
    lon: float
    radius_nm: float
    interval_s: float
    tier: str


@dataclass(frozen=True)
class PlannerConfig:
    inner_margin_nm: float = 25.0
    inner_interval_s: float = 10.0
    outer_enabled: bool = False
    horizon_min: float = 10.0
    outer_speed_nm_per_min: float = 8.0  # ~480 kt
    outer_interval_s: float = 30.0


def _enclosing(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Smallest circle containing circles a and b (lat, lon, r)."""
    d = geo.haversine_nm(a[0], a[1], b[0], b[1])
    if d + b[2] <= a[2]:
        return a
    if d + a[2] <= b[2]:
        return b
    r = (d + a[2] + b[2]) / 2
    # Center lies on the a→b line, (r - ra) from a.
    brg = geo.bearing_deg(a[0], a[1], b[0], b[1])
    lat, lon = geo.destination(a[0], a[1], brg, r - a[2])
    return lat, lon, r


def merge_circles(circles: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    out = sorted(circles, key=lambda c: -c[2])
    changed = True
    while changed:
        changed = False
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                enc = _enclosing(out[i], out[j])
                if enc[2] <= max(out[i][2], out[j][2]) * MERGE_SLACK:
                    out[i] = (enc[0], enc[1], min(MAX_RADIUS_NM, enc[2]))
                    del out[j]
                    changed = True
                    break
            if changed:
                break
    return out


def plan(places: list[Place], now: float, cfg: PlannerConfig) -> list[QueryCircle]:
    active = [p for p in places if p.is_active(now)]
    tiers: list[tuple[str, float, float]] = [("inner", cfg.inner_margin_nm, cfg.inner_interval_s)]
    if cfg.outer_enabled:
        reach = cfg.horizon_min * cfg.outer_speed_nm_per_min
        tiers.append(("outer", max(reach, cfg.inner_margin_nm * 1.5), cfg.outer_interval_s))
    result: list[QueryCircle] = []
    for tier, margin, interval in tiers:
        raw = [(p.lat, p.lon, min(MAX_RADIUS_NM, p.radius_nm + margin)) for p in active]
        for lat, lon, r in merge_circles(raw):
            key = f"{tier}:{lat:.3f},{lon:.3f},{math.ceil(r)}"
            result.append(QueryCircle(key, lat, lon, math.ceil(r), interval, tier))
    return result
