"""Filed flights from FAA SWIM (TFMS), kept only for your airport places and followed aircraft.

Bounded in memory (`MAX_FLIGHTS`, oldest first out) and in SQLite (`KEEP_S` retention). Matched
to live aircraft by callsign, or by registration for general aviation (N-numbers file as their
aircraft id). TFMS carries neither the ICAO hex nor a registration field.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields, replace
from typing import Any

from app.db import Database
from app.enrich.routes import Route, RouteLeg
from app.sources.swim import FlightUpdate, airport_aliases, normalize_acid

log = logging.getLogger(__name__)

KEEP_S = 3 * 86400.0
MAX_FLIGHTS = 20_000
MAX_AIRPORT_CACHE = 5_000
# A plan matches a live aircraft from this long before its departure time …
MATCH_BEFORE_DEPARTURE_S = 2 * 3600.0
# … until this long after its arrival time (or departure + MAX_FLIGHT_S if no arrival is known).
MATCH_AFTER_ARRIVAL_S = 1800.0
MAX_FLIGHT_S = 18 * 3600.0


@dataclass(frozen=True, slots=True)
class AirportRef:
    code: str  # ICAO when the airport has one, else its ident
    name: str | None
    lat: float
    lon: float


@dataclass(slots=True)
class FiledFlight:
    key: str
    acid: str
    updated_at: float  # newest sourceTimeStamp applied
    msg_type: str = ""
    origin: str | None = None  # ICAO code when resolvable (KDEN), else as published
    destination: str | None = None
    gufi: str | None = None
    aircraft_type: str | None = None
    status: str | None = None
    scheduled_departure: float | None = None
    etd: float | None = None
    etd_type: str | None = None
    eta: float | None = None
    eta_type: str | None = None
    departed_at: float | None = None
    arrived_at: float | None = None
    cancelled: bool = False

    @property
    def departure_time(self) -> float | None:
        """Best known departure: actual, else estimated/proposed, else the scheduled gate time."""
        return self.departed_at or self.etd or self.scheduled_departure

    @property
    def arrival_time(self) -> float | None:
        return self.arrived_at or self.eta

    @property
    def last_relevant(self) -> float:
        return max(self.updated_at, self.arrival_time or 0.0, self.departure_time or 0.0)

    def apply(self, u: FlightUpdate) -> bool:
        """Merge an update. Older messages (TFMS delivers some late) only fill in gaps."""
        newer = u.sent_at >= self.updated_at
        changed = False
        for name in _MERGED:
            value = getattr(u, name)
            if value is None or value is False:
                continue
            current = getattr(self, name)
            if (newer or current in (None, False)) and current != value:
                setattr(self, name, value)
                changed = True
        if u.acid != self.acid and newer:
            self.acid, changed = u.acid, True
        if newer:
            self.updated_at = u.sent_at
            self.msg_type = u.msg_type
        return changed

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FiledFlight:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

    def summary(self) -> dict[str, Any]:
        return {**self.to_json(), "departure_time": self.departure_time,
                "arrival_time": self.arrival_time}  # fmt: skip


_MERGED = (
    "origin", "destination", "gufi", "aircraft_type", "status", "scheduled_departure", "etd",
    "etd_type", "eta", "eta_type", "departed_at", "arrived_at", "cancelled",
)  # fmt: skip


def _match_score(f: FiledFlight, t: float) -> float | None:
    """How well the plan fits an aircraft seen at time t: 0 while in progress (departure to
    arrival), else seconds to that span; None outside the matching window or if cancelled."""
    if f.cancelled:
        return None
    dep, arr = f.departure_time, f.arrival_time
    if dep is not None:
        start = dep - MATCH_BEFORE_DEPARTURE_S
        end = (arr if arr is not None else dep + MAX_FLIGHT_S) + MATCH_AFTER_ARRIVAL_S
    elif arr is not None:
        start, end = arr - MAX_FLIGHT_S, arr + MATCH_AFTER_ARRIVAL_S
    else:
        return None
    if not start <= t <= end:
        return None
    lo = dep if dep is not None else start
    hi = arr if arr is not None else end
    return max(0.0, lo - t, t - hi)


class SwimFlights:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.flights: dict[str, FiledFlight] = {}
        self.by_acid: dict[str, set[str]] = {}
        self.airports: dict[str, AirportRef | None] = {}
        self.dirty: set[str] = set()
        self.evicted = 0

    # ---- persistence -------------------------------------------------------------------------

    async def load(self, now: float) -> None:
        rows = await self.db.fetchall(
            "SELECT data_json FROM swim_flights WHERE updated_at >= ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (now - KEEP_S, MAX_FLIGHTS),
        )
        for r in rows:
            try:
                f = FiledFlight.from_json(json.loads(r["data_json"]))
            except (TypeError, ValueError):
                continue
            self._put(f)
        for code in {c for f in self.flights.values() for c in (f.origin, f.destination) if c}:
            await self.airport(code)

    async def flush(self) -> int:
        if not self.dirty:
            return 0
        rows = [
            (f.key, f.acid, f.updated_at, json.dumps(f.to_json()))
            for k in self.dirty
            if (f := self.flights.get(k)) is not None
        ]
        self.dirty.clear()
        await self.db.executemany(
            "INSERT OR REPLACE INTO swim_flights (key, acid, updated_at, data_json) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        return len(rows)

    async def prune(self, now: float) -> int:
        cutoff = now - KEEP_S
        old = [k for k, f in self.flights.items() if f.last_relevant < cutoff]
        for k in old:
            self._drop(k)
        await self.db.execute("DELETE FROM swim_flights WHERE updated_at < ?", (cutoff,))
        return len(old)

    # ---- updates -----------------------------------------------------------------------------

    async def airport(self, code: str) -> AirportRef | None:
        """Resolve a TFMS airport code (ICAO, or FAA like DEN) through OurAirports, cached."""
        if code in self.airports:
            return self.airports[code]
        if len(self.airports) >= MAX_AIRPORT_CACHE:
            self.airports.clear()
        candidates = [code] if len(code) != 3 else [f"K{code}", f"P{code}", code]
        ref: AirportRef | None = None
        for c in candidates:
            row = await self.db.fetchone(
                "SELECT ident, icao, name, lat, lon FROM airports WHERE icao = ? OR ident = ? "
                "ORDER BY icao IS NULL LIMIT 1",
                (c, c),
            )
            if row is not None:
                ref = AirportRef(row["icao"] or row["ident"], row["name"], row["lat"], row["lon"])
                break
        self.airports[code] = ref
        if ref is not None:
            self.airports[ref.code] = ref  # flights store the resolved code
        return ref

    async def apply(self, updates: list[FlightUpdate]) -> list[FiledFlight]:
        changed: dict[str, FiledFlight] = {}
        for u in updates:
            origin = await self._code(u.origin)
            destination = await self._code(u.destination)
            if (origin, destination) != (u.origin, u.destination):
                u = replace(u, origin=origin, destination=destination)
            f = self.flights.get(u.key)
            if f is None:
                f = FiledFlight(u.key, u.acid, u.sent_at, u.msg_type)
                f.apply(u)
                self._put(f)
            else:
                old_acid = f.acid
                if not f.apply(u):
                    continue
                if f.acid != old_acid:
                    self._unindex(u.key, old_acid)
                    self.by_acid.setdefault(f.acid, set()).add(u.key)
            changed[u.key] = f
            self.dirty.add(u.key)
        self._evict()
        return list(changed.values())

    async def _code(self, code: str | None) -> str | None:
        if not code:
            return None
        ref = await self.airport(code)
        return ref.code if ref else code

    def _put(self, f: FiledFlight) -> None:
        self.flights[f.key] = f
        self.by_acid.setdefault(f.acid, set()).add(f.key)

    def _unindex(self, key: str, acid: str) -> None:
        keys = self.by_acid.get(acid)
        if keys is not None:
            keys.discard(key)
            if not keys:
                del self.by_acid[acid]

    def _drop(self, key: str) -> None:
        f = self.flights.pop(key, None)
        if f is not None:
            self._unindex(key, f.acid)
        self.dirty.discard(key)

    def _evict(self) -> None:
        over = len(self.flights) - MAX_FLIGHTS
        if over <= 0:
            return
        oldest = sorted(self.flights.values(), key=lambda f: f.last_relevant)
        for f in oldest[: over + MAX_FLIGHTS // 10]:
            self._drop(f.key)
            self.evicted += 1

    # ---- queries -----------------------------------------------------------------------------

    def best_for(
        self, callsign: str | None, registration: str | None, t: float
    ) -> FiledFlight | None:
        """The plan this aircraft is flying at time t, by callsign or (GA) registration."""
        keys: set[str] = set()
        for ident in {normalize_acid(callsign), normalize_acid(registration)}:
            if ident:
                keys |= self.by_acid.get(ident, set())
        best: tuple[float, float, FiledFlight] | None = None
        for k in keys:
            f = self.flights.get(k)
            if f is None or (score := _match_score(f, t)) is None:
                continue
            if best is None or (score, -f.updated_at) < (best[0], best[1]):
                best = (score, -f.updated_at, f)
        return best[2] if best else None

    def route_for(self, f: FiledFlight) -> Route | None:
        a = self.airports.get(f.origin or "") if f.origin else None
        b = self.airports.get(f.destination or "") if f.destination else None
        if a is None or b is None or a.code == b.code:
            return None
        legs = (RouteLeg(a.code, None, a.name, a.lat, a.lon),
                RouteLeg(b.code, None, b.name, b.lat, b.lon))  # fmt: skip
        return Route(f.acid, legs, True, "faa-swim", f.updated_at)

    def upcoming(self, codes: set[str], now: float, limit: int = 50) -> list[FiledFlight]:
        """Flights to or from these airports that haven't arrived yet, soonest first."""
        aliases = {a for c in codes for a in airport_aliases(c)}
        out = [
            f for f in self.flights.values()
            if not f.cancelled and f.arrived_at is None
            and (f.origin in aliases or f.destination in aliases)
            and (f.arrival_time or f.departure_time or 0.0) >= now - MATCH_AFTER_ARRIVAL_S
        ]  # fmt: skip
        out.sort(key=lambda f: f.departure_time or f.arrival_time or 0.0)
        return out[:limit]
