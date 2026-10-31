"""FAA special use airspace (restricted, prohibited, warning, alert areas and MOAs) for drawing
area places. FAA ADDS open data, US public domain. Queried only when the user searches, and cached.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from app import geo
from app.clock import Clock
from app.db import Database

SUA_URL = (
    "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/"
    "Special_Use_Airspace/FeatureServer/0/query"
)
ATTRIBUTION = "FAA ADDS special use airspace (public domain)"
CACHE_TTL_S = 7 * 86400.0
MAX_RESULTS = 30
MAX_POINTS = 200  # Place.polygon allows 500
MIN_INTERVAL_S = 1.0
TYPE_NAMES = {
    "R": "Restricted", "P": "Prohibited", "W": "Warning", "A": "Alert", "MOA": "MOA",
    "D": "Danger",
}  # fmt: skip
OUT_FIELDS = (
    "NAME,TYPE_CODE,UPPER_VAL,UPPER_UOM,UPPER_CODE,LOWER_VAL,LOWER_UOM,LOWER_CODE,"
    "TIMESOFUSE,GMTOFFSET,DST_CODE,CITY,STATE,CONT_AGENT"
)
# FAA gives a GMT offset and whether DST applies; times of use are local to the airspace.
_ZONES = {
    (-4, False): "America/Puerto_Rico", (-5, True): "America/New_York",
    (-6, True): "America/Chicago", (-7, True): "America/Denver", (-7, False): "America/Phoenix",
    (-8, True): "America/Los_Angeles", (-9, True): "America/Anchorage",
    (-10, False): "Pacific/Honolulu", (10, False): "Pacific/Guam",
}  # fmt: skip
_SAFE_QUERY = re.compile(r"[^A-Z0-9 \-]")
_DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
_DAY = r"(MON|TUE|WED|THU|FRI|SAT|SUN)"
_TIMES = re.compile(
    rf"^(\d{{4}}) - (\d{{4}}), (DAILY|{_DAY}(?: - {_DAY})?)"
    r"(?:$|;\s*(?:OTHER TIMES|O/T) BY NOTAM.*$)"
)


class AirspaceError(Exception):
    pass


@dataclass
class Limit:
    ft: float | None  # None = surface (floor) or unlimited (ceiling)
    ref: str  # "MSL", "AGL" (add the ground elevation), "SFC" or "UNL"
    text: str


@dataclass
class Airspace:
    name: str
    type: str
    floor: Limit
    ceiling: Limit
    polygon: list[tuple[float, float]]  # (lat, lon)
    center: tuple[float, float]
    radius_nm: float  # covers the whole polygon
    times_of_use: str | None
    schedule: dict[str, Any] | None  # {days, start, end, tz}: only when times and zone are known
    location: str
    controlling_agency: str | None
    parts_dropped: int = 0  # extra rings (separate parts) not imported

    def public(self) -> dict[str, Any]:
        return asdict(self)


def altitude(val: str | None, uom: str | None, code: str | None, is_floor: bool) -> Limit:
    try:
        v = float(val) if val is not None else 0.0
    except ValueError:
        v = 0.0
    if code == "UNLTD" or v <= -9000:
        return Limit(None, "UNL", "unlimited")
    if code == "STD" or uom == "FL":
        return Limit(v * 100, "MSL", f"FL{v:03.0f}")
    if code == "SFC":
        if v <= 0:
            return Limit(None if is_floor else 0.0, "SFC", "surface")
        return Limit(v, "AGL", f"{v:,.0f} ft AGL")
    return Limit(v, "MSL", f"{v:,.0f} ft MSL")


def parse_schedule(text: str | None) -> dict[str, Any] | None:
    """Only the common single-window form, e.g. '0900 - 1700, MON - SAT; O/T BY NOTAM'."""
    if not text:
        return None
    m = _TIMES.match(text.strip().upper())
    if m is None:
        return None
    start, end, days_text, first, last = m.groups()
    if days_text == "DAILY":
        days = list(range(7))
    else:
        a = _DAYS.index(first)
        b = _DAYS.index(last) if last else a
        days = sorted(d % 7 for d in range(a, (b if b >= a else b + 7) + 1))
    if int(start[:2]) > 24 or int(end[:2]) > 24:
        return None
    return {"days": days, "start": f"{start[:2]}:{start[2:]}", "end": f"{end[:2]}:{end[2:]}"}


def time_zone(offset: str | None, dst: str | None) -> str | None:
    try:
        return _ZONES.get((int(float(offset or "")), dst == "1"))
    except ValueError:
        return None


def _ring_area(ring: list[list[float]]) -> float:
    return (
        abs(
            sum(
                x1 * y2 - x2 * y1
                for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True)
            )
        )
        / 2
    )


def _simplify(points: list[tuple[float, float]], limit: int) -> list[tuple[float, float]]:
    """Douglas-Peucker with a growing tolerance until it fits; keeps the outline's shape."""
    tol = 0.0005
    out = points
    while len(out) > limit:
        out = _dp(points, tol)
        tol *= 2
    return out


def _dp(pts: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    if len(pts) < 3:
        return pts
    (ax, ay), (bx, by) = pts[0], pts[-1]
    dx, dy = bx - ax, by - ay
    norm = math.hypot(dx, dy) or 1e-12
    best, idx = -1.0, 0
    for i in range(1, len(pts) - 1):
        px, py = pts[i]
        d = abs(dy * (px - ax) - dx * (py - ay)) / norm
        if d > best:
            best, idx = d, i
    if best <= tol:
        return [pts[0], pts[-1]]
    return _dp(pts[: idx + 1], tol)[:-1] + _dp(pts[idx:], tol)


def _polygon(rings: list[list[list[float]]]) -> tuple[list[tuple[float, float]], int]:
    """Largest ring as (lat, lon) without the closing point; and how many parts were left out."""
    if not rings:
        return [], 0
    ring = max(rings, key=_ring_area)
    pts = [(round(lat, 5), round(lon, 5)) for lon, lat in ring]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return _simplify(pts, MAX_POINTS), len(rings) - 1


def _airspace(attrs: dict[str, Any], rings: list[list[list[float]]]) -> Airspace | None:
    polygon, dropped = _polygon(rings)
    if len(polygon) < 3:
        return None
    lat = sum(p[0] for p in polygon) / len(polygon)
    lon = sum(p[1] for p in polygon) / len(polygon)
    radius = max(geo.haversine_nm(lat, lon, a, b) for a, b in polygon)
    times = (attrs.get("TIMESOFUSE") or "").strip() or None
    schedule = parse_schedule(times)
    if schedule is not None:
        zone = time_zone(attrs.get("GMTOFFSET"), attrs.get("DST_CODE"))
        schedule = schedule | {"tz": zone} if zone else None
    city, state = attrs.get("CITY"), attrs.get("STATE")
    where = ", ".join(x for x in ((city or "").title(), state or "") if x)
    return Airspace(
        name=attrs.get("NAME") or "?",
        type=TYPE_NAMES.get(attrs.get("TYPE_CODE") or "", attrs.get("TYPE_CODE") or "?"),
        floor=altitude(
            attrs.get("LOWER_VAL"), attrs.get("LOWER_UOM"), attrs.get("LOWER_CODE"), True
        ),
        ceiling=altitude(
            attrs.get("UPPER_VAL"), attrs.get("UPPER_UOM"), attrs.get("UPPER_CODE"), False
        ),
        polygon=polygon,
        center=(round(lat, 5), round(lon, 5)),
        radius_nm=round(radius + 1, 1),
        times_of_use=times,
        schedule=schedule,
        location=where,
        controlling_agency=attrs.get("CONT_AGENT"),
        parts_dropped=dropped,
    )


def _merge(items: list[Airspace]) -> list[Airspace]:
    """Stacked sectors with the same outline (e.g. R-3601A below FL180, R-3601B above) become one
    result when their bands touch, named 'R-3601A/B'."""
    out: list[Airspace] = []
    for a in sorted(items, key=lambda x: (x.name, x.floor.ft or 0)):
        prev = next(
            (o for o in out if o.polygon == a.polygon and o.ceiling.ft is not None
             and o.ceiling.ref == "MSL" and a.floor.ref == "MSL" and o.ceiling.ft == a.floor.ft),
            None,
        )  # fmt: skip
        if prev is None:
            out.append(a)
            continue
        common = _common_prefix(prev.name, a.name)
        prev.name = f"{prev.name}/{a.name[len(common) :]}" if common else f"{prev.name} + {a.name}"
        prev.ceiling = a.ceiling
        if prev.times_of_use != a.times_of_use:
            prev.times_of_use = "; ".join(t for t in (prev.times_of_use, a.times_of_use) if t)
            prev.schedule = None
    return out


def _common_prefix(a: str, b: str) -> str:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return a[:n]


class AirspaceSearch:
    def __init__(
        self, db: Database, http: httpx.AsyncClient, clock: Clock, user_agent: str
    ) -> None:
        self.db = db
        self.http = http
        self.clock = clock
        self.user_agent = user_agent
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def by_name(self, q: str) -> list[Airspace]:
        term = _SAFE_QUERY.sub("", q.upper()).strip()
        if len(term) < 2:
            return []
        return await self._query(f"name:{term}", {"where": f"NAME LIKE '%{term}%'"})

    async def near(self, lat: float, lon: float, radius_nm: float) -> list[Airspace]:
        r = min(max(radius_nm, 1.0), 100.0)
        dlat = r / 60.0
        dlon = r / (60.0 * max(0.1, math.cos(math.radians(lat))))
        box = f"{lon - dlon:.4f},{lat - dlat:.4f},{lon + dlon:.4f},{lat + dlat:.4f}"
        params = {
            "where": "1=1", "geometry": box, "geometryType": "esriGeometryEnvelope",
            "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
        }  # fmt: skip
        found = await self._query(f"near:{box}", params)
        return sorted(found, key=lambda a: geo.haversine_nm(lat, lon, *a.center))

    async def _query(self, key: str, params: dict[str, str]) -> list[Airspace]:
        now = self.clock.now()
        row = await self.db.fetchone(
            "SELECT value, fetched_at FROM cache WHERE namespace = 'faa-sua' AND key = ?", (key,)
        )
        if row is not None and now - row["fetched_at"] < CACHE_TTL_S:
            features = json.loads(row["value"])
        else:
            features = await self._fetch(params)
            await self.db.execute(
                "INSERT OR REPLACE INTO cache(namespace, key, value, fetched_at) "
                "VALUES ('faa-sua', ?, ?, ?)",
                (key, json.dumps(features), now),
            )
        items = [
            a for f in features
            if (a := _airspace(f.get("attributes", {}), f.get("geometry", {}).get("rings", [])))
        ]  # fmt: skip
        return _merge(items)[:MAX_RESULTS]

    async def _fetch(self, params: dict[str, str]) -> list[dict[str, Any]]:
        async with self._lock:
            wait = MIN_INTERVAL_S - (self.clock.now() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = self.clock.now()
            query = {
                **params, "outFields": OUT_FIELDS, "returnGeometry": "true", "outSR": "4326",
                "geometryPrecision": "5", "resultRecordCount": "50", "f": "json",
            }  # fmt: skip
            try:
                resp = await self.http.get(
                    SUA_URL, params=query, headers={"User-Agent": self.user_agent}, timeout=20
                )
                data = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                raise AirspaceError(f"FAA airspace service unreachable: {e}") from e
            if resp.status_code != 200 or "error" in data:
                raise AirspaceError(f"FAA airspace service error ({resp.status_code})")
            features: list[dict[str, Any]] = data.get("features", [])
            return features
