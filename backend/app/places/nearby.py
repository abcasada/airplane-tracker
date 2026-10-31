"""Nearest airport to an aircraft, to say where a far-away aircraft is ("12 nm SW of KDOV")."""

from __future__ import annotations

import math
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


class AirportIndex:
    """Large and medium airports (~5k worldwide) in memory; a scan is a few milliseconds and only
    runs for aircraft whose location is being described."""

    def __init__(self) -> None:
        self.items: list[tuple[float, float, str, str, str | None]] = []

    async def load(self, db: Database) -> None:
        rows = await db.fetchall(
            "SELECT ident, icao, name, municipality, lat, lon FROM airports "
            "WHERE type IN ('large_airport', 'medium_airport')"
        )
        self.items = [
            (r["lat"], r["lon"], r["icao"] or r["ident"], r["name"], r["municipality"])
            for r in rows
        ]

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
