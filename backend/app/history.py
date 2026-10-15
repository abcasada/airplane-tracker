"""Sightings history: which days each aircraft (and each type) came near each place.

Today's sightings are kept in memory and flushed every minute. Past days are loaded lazily per
aircraft and per type, into bounded caches, so the history fields stay cheap synchronous lookups.
Queries take the day to count back from, so replays of past days see only the history before them.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app import geo
from app.db import Database
from app.places.models import Place
from app.tracks.store import Track

log = logging.getLogger(__name__)

RETENTION_DAYS = 180
WINDOW_DAYS = 30
FLUSH_INTERVAL_S = 60.0
MAX_HEX_CACHE = 5000
MAX_TYPE_CACHE = 1000
_QUERY_CHUNK = 400  # stays under SQLite's bound-parameter limit


@dataclass
class _Today:
    type_code: str | None
    first_at: float
    last_at: float
    min_dist_nm: float


def day_of(now: float, tz: ZoneInfo) -> int:
    """Local date as an ordinal, so day arithmetic is plain integers."""
    return datetime.fromtimestamp(now, tz).date().toordinal()


def _iso(day: int) -> str:
    return date.fromordinal(day).isoformat()


class SightingsBook:
    def __init__(self, db: Database, read_only: bool = False) -> None:
        self.db = db
        self.read_only = read_only
        self.today: dict[tuple[str, int, int], _Today] = {}
        self._dirty: set[tuple[str, int, int]] = set()
        self._hex: OrderedDict[str, dict[int, set[int]]] = OrderedDict()
        self._type: OrderedDict[tuple[str, int], set[int]] = OrderedDict()
        self._loaded_from: int | None = None  # earliest day the caches cover
        self._last_flush = 0.0

    # ---- recording -----------------------------------------------------------------------------

    def record(
        self, tracks: Iterable[Track], places: list[Place], now: float, tz: ZoneInfo
    ) -> None:
        day = day_of(now, tz)
        for t in tracks:
            if t.lat is None or t.lon is None:
                continue
            for p in places:
                d = geo.haversine_nm(p.lat, p.lon, t.lat, t.lon)
                if d > p.radius_nm:
                    continue
                key = (t.hex, p.id, day)
                s = self.today.get(key)
                if s is None:
                    self.today[key] = _Today(t.type_code, now, now, d)
                    # Keep loaded caches current, so tomorrow sees today without a reload.
                    if (by_place := self._hex.get(t.hex)) is not None:
                        by_place.setdefault(p.id, set()).add(day)
                    if t.type_code and (t.type_code, -1) in self._type:
                        self._type.setdefault((t.type_code, p.id), set()).add(day)
                else:
                    s.last_at, s.min_dist_nm = now, min(s.min_dist_nm, d)
                    s.type_code = s.type_code or t.type_code
                self._dirty.add(key)

    async def maybe_flush(self, now: float) -> None:
        if now - self._last_flush >= FLUSH_INTERVAL_S:
            await self.flush(now)

    async def flush(self, now: float) -> None:
        self._last_flush = now
        if self.read_only or not self._dirty:
            return
        rows = []
        for key in self._dirty:
            s = self.today.get(key)
            if s is not None:
                rows.append((key[0], key[1], _iso(key[2]), s.type_code, s.first_at, s.last_at,
                             round(s.min_dist_nm, 2)))  # fmt: skip
        self._dirty.clear()
        await self.db.executemany(
            "INSERT INTO sightings(hex, place_id, day, type_code, first_at, last_at, min_dist_nm) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(hex, place_id, day) DO UPDATE SET "
            "type_code = coalesce(sightings.type_code, excluded.type_code), "
            "last_at = max(sightings.last_at, excluded.last_at), "
            "min_dist_nm = min(sightings.min_dist_nm, excluded.min_dist_nm)",
            rows,
        )
        # Older days are on disk now; keep only today's in memory.
        current = max((k[2] for k in self.today), default=0)
        self.today = {k: v for k, v in self.today.items() if k[2] == current}

    async def prune(self, now: float, tz: ZoneInfo, place_ids: set[int]) -> int:
        cutoff = _iso(day_of(now, tz) - RETENTION_DAYS)
        n = await self.db.execute("DELETE FROM sightings WHERE day < ?", (cutoff,))
        if place_ids:
            marks = ",".join("?" * len(place_ids))
            n += await self.db.execute(
                f"DELETE FROM sightings WHERE place_id NOT IN ({marks})",  # noqa: S608
                tuple(place_ids),
            )
        return n

    # ---- lookups -------------------------------------------------------------------------------

    async def prepare(self, tracks: Iterable[Track], day: int) -> None:
        """Load history for aircraft and types not cached yet (one query per batch)."""
        start = day - RETENTION_DAYS
        if self._loaded_from is None or start < self._loaded_from:
            self._hex.clear()
            self._type.clear()
            self._loaded_from = start
        tracks = list(tracks)
        hexes = [t.hex for t in tracks if t.hex not in self._hex]
        types = {t.type_code for t in tracks if t.type_code} - {k[0] for k in self._type}
        for i in range(0, len(hexes), _QUERY_CHUNK):
            chunk = hexes[i : i + _QUERY_CHUNK]
            found: dict[str, dict[int, set[int]]] = {h: {} for h in chunk}
            rows = await self.db.fetchall(
                f"SELECT hex, place_id, day FROM sightings WHERE day >= ? "  # noqa: S608
                f"AND hex IN ({','.join('?' * len(chunk))})",
                (_iso(start), *chunk),
            )
            for r in rows:
                found[r["hex"]].setdefault(r["place_id"], set()).add(
                    date.fromisoformat(r["day"]).toordinal()
                )
            for h, by_place in found.items():
                self._hex[h] = by_place
        for type_code in types:
            rows = await self.db.fetchall(
                "SELECT place_id, day FROM sightings WHERE type_code = ? AND day >= ? "
                "GROUP BY place_id, day",
                (type_code, _iso(start)),
            )
            type_days: dict[int, set[int]] = {}
            for r in rows:
                type_days.setdefault(r["place_id"], set()).add(
                    date.fromisoformat(r["day"]).toordinal()
                )
            for pid, days in type_days.items():
                self._type[(type_code, pid)] = days
            self._type.setdefault((type_code, -1), set())  # marks the type as loaded
        while len(self._hex) > MAX_HEX_CACHE:
            self._hex.popitem(last=False)
        while len(self._type) > MAX_TYPE_CACHE:
            self._type.popitem(last=False)

    def _days(self, hex_: str, place_id: int) -> set[int] | None:
        by_place = self._hex.get(hex_)
        if by_place is None:
            return None  # not loaded: unknown rather than "never seen"
        self._hex.move_to_end(hex_)
        return by_place.get(place_id, set())

    def visits_before(
        self, hex_: str, place_id: int, day: int, window: int = WINDOW_DAYS
    ) -> int | None:
        days = self._days(hex_, place_id)
        if days is None:
            return None
        return sum(1 for d in days if day - window <= d < day)

    def days_since_visit(self, hex_: str, place_id: int, day: int) -> int | None:
        days = self._days(hex_, place_id)
        if days is None:
            return None
        before = [d for d in days if d < day]
        return day - max(before) if before else None

    def first_visit(self, hex_: str, place_id: int, day: int) -> bool | None:
        days = self._days(hex_, place_id)
        if days is None:
            return None
        return not any(d < day for d in days)

    def type_visits_before(
        self, type_code: str | None, place_id: int, day: int, window: int = WINDOW_DAYS
    ) -> int | None:
        if not type_code or (type_code, -1) not in self._type:
            return None
        days = self._type.get((type_code, place_id), set())
        return sum(1 for d in days if day - window <= d < day)

    async def stats(self) -> dict[str, int | str | None]:
        row = await self.db.fetchone(
            "SELECT count(*) AS n, count(DISTINCT day) AS days, min(day) AS first FROM sightings"
        )
        return {"rows": row["n"], "days": row["days"], "since": row["first"]} if row else {}
