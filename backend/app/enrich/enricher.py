"""Per-aircraft lookups against the imported datasets, with bounded in-memory caches.

Precedence for registration, type, owner and year (first one that has a value wins):
live source data (adsb.fi/adsb.lol send their own database fields; see `Track`) →
tar1090-db → FAA registry (US civil hex block only) → cached hexdb.io answer.
"""

import bisect
import json
import re
from collections import OrderedDict

from app.clock import Clock, SystemClock
from app.db import Database

from . import faa, hexdb
from .models import AircraftInfo, Airline, PlaneAlertEntry

CACHE_SIZE = 5000
_HEXDB_FIELDS = {"registration", "type_code", "type_desc", "owner_op"}
_AIRLINE_CALLSIGN = re.compile(r"^([A-Z]{3})\d")


class Enricher:
    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock = clock or SystemClock()
        self._cache: OrderedDict[str, AircraftInfo] = OrderedDict()
        self._range_starts: list[int] = []
        self._ranges: list[tuple[int, int]] = []
        self.type_stats: dict[str, tuple[int, float, str | None]] = {}
        self.airlines: dict[str, Airline] = {}

    async def reload(self) -> None:
        """Load the small tables into memory and drop cached lookups (after an import)."""
        self._cache.clear()
        rows = await self.db.fetchall(
            "SELECT start_hex, end_hex FROM mil_ranges ORDER BY start_hex"
        )
        self._ranges = [(r[0], r[1]) for r in rows]
        self._range_starts = [r[0] for r in self._ranges]
        self.type_stats = {
            r["type_code"]: (r["airframes"], r["military_share"], r["descr"])
            for r in await self.db.fetchall("SELECT * FROM type_stats")
        }
        self.airlines = {
            r["icao"]: Airline(r["icao"], r["name"], None, None)
            for r in await self.db.fetchall("SELECT icao, name FROM airlines")
        }

    def in_military_range(self, hex_: str) -> bool:
        try:
            value = int(hex_, 16)
        except ValueError:
            return False
        i = bisect.bisect_right(self._range_starts, value) - 1
        return i >= 0 and self._ranges[i][0] <= value <= self._ranges[i][1]

    def airline_for(self, callsign: str | None) -> Airline | None:
        if not callsign:
            return None
        m = _AIRLINE_CALLSIGN.match(callsign)
        return self.airlines.get(m.group(1)) if m else None

    def type_info(self, type_code: str | None) -> tuple[int, float, str | None] | None:
        return self.type_stats.get(type_code.upper()) if type_code else None

    def forget(self, hex_: str) -> None:
        self._cache.pop(hex_.lower(), None)

    async def _fill_hexdb(self, hex_: str, info: AircraftInfo) -> None:
        row = await self.db.fetchone(
            "SELECT value, fetched_at FROM cache WHERE namespace = ? AND key = ?",
            (hexdb.NAMESPACE, hex_),
        )
        if row is None:
            return
        info.hexdb_checked = hexdb.cache_fresh(row["value"], row["fetched_at"], self.clock.now())
        # A stale answer is still better than nothing until it's refreshed.
        values = json.loads(row["value"]) if row["value"] else None
        if isinstance(values, dict):
            info.fill("hexdb", **{k: v for k, v in values.items() if k in _HEXDB_FIELDS})

    async def lookup(self, hex_: str) -> AircraftInfo:
        hex_ = hex_.lower()
        cached = self._cache.get(hex_)
        if cached is not None:
            self._cache.move_to_end(hex_)
            return cached
        info = AircraftInfo(in_military_range=self.in_military_range(hex_.lstrip("~")))
        row = await self.db.fetchone("SELECT * FROM aircraft_db WHERE icao = ?", (hex_,))
        if row:
            flags = row["flags"] or ""
            info.registration = row["reg"]
            info.type_code = row["type_code"]
            info.type_desc = row["descr"]
            info.owner_op = row["own_op"]
            info.year = row["year"]
            info.db_military = flags[:1] == "1"
            info.db_interesting = flags[1:2] == "1"
            info.db_pia = flags[2:3] == "1"
            info.db_ladd = flags[3:4] == "1"
        pa = await self.db.fetchone(
            "SELECT p.*, i.images FROM plane_alert p LEFT JOIN plane_alert_images i "
            "ON i.icao = p.icao WHERE p.icao = ?",
            (hex_,),
        )
        if pa:
            info.plane_alert = PlaneAlertEntry(
                icao=hex_,
                registration=pa["reg"],
                operator=pa["operator"],
                type_desc=pa["type_desc"],
                icao_type=pa["icao_type"],
                cmpg=pa["cmpg"],
                tags=tuple(t for t in (pa["tag1"], pa["tag2"], pa["tag3"]) if t),
                category=pa["category"],
                link=pa["link"],
                images=tuple(json.loads(pa["images"])) if pa["images"] else (),
            )
        if faa.is_us_civil(hex_):
            row = await self.db.fetchone(faa.LOOKUP_SQL, (hex_,))
            if row:
                rec = faa.record_from_row(row)
                info.faa = rec
                info.fill("faa", registration=rec.n_number, type_desc=rec.type_desc,
                          owner_op=rec.name, year=rec.year)  # fmt: skip
        if not (info.registration and info.type_code and info.owner_op):
            await self._fill_hexdb(hex_, info)
        self._cache[hex_] = info
        if len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return info
