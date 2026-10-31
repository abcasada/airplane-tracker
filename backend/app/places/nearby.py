"""Nearest airport to an aircraft, to say where a far-away aircraft is ("12 nm SW of KDOV")."""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass

from app import geo
from app.db import Database

AT_AIRPORT_NM = 3.0


@dataclass(frozen=True, slots=True)
class NearAirport:
    code: str
    name: str
    municipality: str | None
    distance_nm: float
    bearing_deg: float  # from the airport to the aircraft

    def describe(self) -> str:
        label = f"{self.name} ({self.code})"
        if self.distance_nm < AT_AIRPORT_NM:
            return f"at {label}"
        compass = geo.compass16(self.bearing_deg)
        return f"{self.distance_nm:.0f} nm {compass} of {label}"


@dataclass(frozen=True, slots=True)
class Airfield:
    code: str
    name: str
    lat: float
    lon: float
    elevation_ft: float | None
    kind: str  # large_airport, medium_airport, small_airport, heliport


_CELL_DEG = 0.5


class AirportIndex:
    """Airports in memory. Large and medium ones (~5k) describe where an aircraft is; every
    airfield including small airports and heliports (~70k, a 0.5° grid) is used to say where a
    flight took off or landed."""

    def __init__(self) -> None:
        self.items: list[tuple[float, float, str, str, str | None]] = []
        self.grid: dict[tuple[int, int], list[Airfield]] = {}

    async def load(self, db: Database) -> None:
        items: list[tuple[float, float, str, str, str | None]] = []
        grid: dict[tuple[int, int], list[Airfield]] = {}
        last = ""
        while True:  # in chunks, so loading ~70k rows doesn't double memory for a moment
            rows = await db.fetchall(
                "SELECT ident, icao, name, municipality, lat, lon, elevation_ft, type "
                "FROM airports WHERE ident > ? AND type IN "
                "('large_airport', 'medium_airport', 'small_airport', 'heliport') "
                "ORDER BY ident LIMIT 5000",
                (last,),
            )
            if not rows:
                break
            last = rows[-1]["ident"]
            for r in rows:
                code = sys.intern(r["icao"] or r["ident"])
                kind = sys.intern(r["type"])
                if kind in ("large_airport", "medium_airport"):
                    items.append((r["lat"], r["lon"], code, r["name"], r["municipality"]))
                field = Airfield(code, r["name"], r["lat"], r["lon"], r["elevation_ft"], kind)
                grid.setdefault(_cell(r["lat"], r["lon"]), []).append(field)
        self.items, self.grid = items, grid

    def nearest(self, lat: float, lon: float) -> NearAirport | None:
        if not self.items:
            return None
        coslat = math.cos(math.radians(lat))
        best = min(self.items, key=lambda a: (a[0] - lat) ** 2 + ((a[1] - lon) * coslat) ** 2)
        a_lat, a_lon, code, name, muni = best
        return NearAirport(
            code, name, muni, geo.haversine_nm(a_lat, a_lon, lat, lon),
            geo.bearing_deg(a_lat, a_lon, lat, lon),
        )  # fmt: skip

    def airfield_near(
        self, lat: float, lon: float, max_nm: float, heliports: bool = True
    ) -> tuple[Airfield, float] | None:
        """Closest airfield within max_nm (≤ ~25 nm), with its distance."""
        cy, cx = _cell(lat, lon)
        best: tuple[Airfield, float] | None = None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for f in self.grid.get((cy + dy, cx + dx), ()):
                    if not heliports and f.kind == "heliport":
                        continue
                    d = geo.haversine_nm(lat, lon, f.lat, f.lon)
                    if d <= max_nm and (best is None or d < best[1]):
                        best = (f, d)
        return best


def _cell(lat: float, lon: float) -> tuple[int, int]:
    return math.floor(lat / _CELL_DEG), math.floor(lon / _CELL_DEG)
