"""Claimed routes by callsign, from the adsb.im route API, with our own plausibility check.

Callsign route databases are often stale (callsigns get reused), so a route only counts when
the aircraft is actually near one of its legs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from app import geo
from app.clock import Clock
from app.db import Database

log = logging.getLogger(__name__)

ROUTESET_URL = "https://adsb.im/api/0/routeset"
BATCH_SIZE = 100
MIN_REQUEST_INTERVAL_S = 30.0
KNOWN_TTL_S = 24 * 3600.0
UNKNOWN_TTL_S = 6 * 3600.0
MEMORY_LIMIT = 20_000


@dataclass(frozen=True, slots=True)
class RouteLeg:
    icao: str
    iata: str | None
    name: str | None
    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class Route:
    callsign: str
    legs: tuple[RouteLeg, ...]  # empty when the database doesn't know the callsign
    source_plausible: bool | None
    source: str
    fetched_at: float

    @property
    def known(self) -> bool:
        return len(self.legs) >= 2


@dataclass(frozen=True, slots=True)
class RouteFit:
    origin: str
    destination: str
    plausible: bool
    off_track_nm: float


def fit_route(route: Route, lat: float | None, lon: float | None) -> RouteFit | None:
    """Pick the leg the aircraft is flying and judge whether the claim is believable."""
    if not route.known:
        return None
    if lat is None or lon is None:
        a, b = route.legs[0], route.legs[-1]
        return RouteFit(a.icao, b.icao, bool(route.source_plausible), float("inf"))
    best: tuple[float, RouteLeg, RouteLeg] | None = None
    for a, b in zip(route.legs, route.legs[1:], strict=False):
        leg_len = geo.haversine_nm(a.lat, a.lon, b.lat, b.lon)
        xt, at = geo.cross_and_along_track_nm(lat, lon, a.lat, a.lon, b.lat, b.lon)
        if at < 0:
            off = geo.haversine_nm(lat, lon, a.lat, a.lon)
        elif at > leg_len:
            off = geo.haversine_nm(lat, lon, b.lat, b.lon)
        else:
            off = abs(xt)
        if best is None or off < best[0]:
            best = (off, a, b)
    assert best is not None
    off, a, b = best
    leg_len = geo.haversine_nm(a.lat, a.lon, b.lat, b.lon)
    _, at = geo.cross_and_along_track_nm(lat, lon, a.lat, a.lon, b.lat, b.lon)
    # Flights stay close to the line near their airports but wander mid-route (airways, winds),
    # so the allowed deviation grows with distance from the nearer end: 30 nm → 150 nm.
    from_end = max(0.0, min(at, leg_len - at))
    allowed = min(150.0, 30.0 + 0.1 * from_end)
    plausible = off <= allowed and route.source_plausible is not False
    return RouteFit(a.icao, b.icao, plausible, off)


class RouteService:
    def __init__(self, db: Database, http: httpx.AsyncClient, clock: Clock) -> None:
        self.db = db
        self.http = http
        self.clock = clock
        self.routes: dict[str, Route] = {}
        self.pending: dict[str, tuple[float, float]] = {}
        self.last_request_at = 0.0
        self.last_error: str | None = None
        self.requests = 0

    async def load(self) -> None:
        since = self.clock.now() - KNOWN_TTL_S
        rows = await self.db.fetchall(
            "SELECT * FROM routes WHERE fetched_at >= ? ORDER BY fetched_at DESC LIMIT ?",
            (since, MEMORY_LIMIT),
        )
        for r in rows:
            self.routes[r["callsign"]] = _route_from_row(r)

    def get(self, callsign: str | None) -> Route | None:
        return self.routes.get(callsign) if callsign else None

    def want(self, callsign: str | None, lat: float | None, lon: float | None) -> None:
        if not callsign or lat is None or lon is None or len(callsign) < 3:
            return
        r = self.routes.get(callsign)
        now = self.clock.now()
        if r is not None and now - r.fetched_at < (KNOWN_TTL_S if r.known else UNKNOWN_TTL_S):
            return
        self.pending[callsign] = (lat, lon)

    async def flush(self) -> int:
        """Look up pending callsigns in one batch. Returns how many were resolved."""
        now = self.clock.now()
        if not self.pending or now - self.last_request_at < MIN_REQUEST_INTERVAL_S:
            return 0
        batch = list(self.pending.items())[:BATCH_SIZE]
        self.last_request_at = now
        body = {"planes": [{"callsign": cs, "lat": la, "lng": lo} for cs, (la, lo) in batch]}
        self.requests += 1
        try:
            resp = await self.http.post(ROUTESET_URL, json=body, timeout=20)
            data = resp.json() if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError) as e:
            self.last_error = str(e)
            log.warning("route lookup failed: %s", e)
            return 0
        if not isinstance(data, list):
            self.last_error = f"HTTP {resp.status_code}"
            return 0
        self.last_error = None
        by_cs = {
            str(item.get("callsign", "")).upper(): item for item in data if isinstance(item, dict)
        }
        rows = []
        for cs, _pos in batch:
            self.pending.pop(cs, None)
            route = _route_from_api(cs, by_cs.get(cs), now)
            self.routes[cs] = route
            rows.append(
                (
                    cs,
                    json.dumps([asdict(leg) for leg in route.legs]) if route.known else None,
                    None if route.source_plausible is None else int(route.source_plausible),
                    route.source,
                    now,
                )
            )
        await self.db.executemany("INSERT OR REPLACE INTO routes VALUES (?, ?, ?, ?, ?)", rows)
        if len(self.routes) > MEMORY_LIMIT:
            for k in sorted(self.routes, key=lambda c: self.routes[c].fetched_at)[
                : MEMORY_LIMIT // 4
            ]:
                del self.routes[k]
        return len(batch)


def _route_from_api(callsign: str, item: dict[str, Any] | None, now: float) -> Route:
    legs: list[RouteLeg] = []
    plausible: bool | None = None
    if item:
        for a in item.get("_airports") or []:
            try:
                legs.append(
                    RouteLeg(
                        str(a.get("icao") or "").upper(),
                        a.get("iata"),
                        a.get("name"),
                        float(a["lat"]),
                        float(a["lon"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                legs = []
                break
        p = item.get("plausible")
        plausible = bool(p) if isinstance(p, bool | int) else None
    return Route(callsign, tuple(legs) if len(legs) >= 2 else (), plausible, "adsb.im", now)


def _route_from_row(r: Any) -> Route:
    legs = tuple(RouteLeg(**leg) for leg in json.loads(r["legs"])) if r["legs"] else ()
    plausible = None if r["plausible"] is None else bool(r["plausible"])
    return Route(r["callsign"], legs, plausible, r["source"], r["fetched_at"])
