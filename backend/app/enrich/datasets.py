"""Download and import bulk reference datasets. Nothing here is committed to the repo.

Each import runs in a worker thread on its own SQLite connection, inside one transaction,
so readers keep seeing the previous version until the new one is complete.
"""

import asyncio
import csv
import gzip
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.clock import Clock
from app.db import Database, connect_sync

log = logging.getLogger(__name__)

TAR1090_AIRCRAFT = "https://raw.githubusercontent.com/wiedehopf/tar1090-db/csv/aircraft.csv.gz"
TAR1090_RANGES = "https://raw.githubusercontent.com/wiedehopf/tar1090-db/master/ranges.json"
PLANE_ALERT = (
    "https://raw.githubusercontent.com/sdr-enthusiasts/plane-alert-db/main/plane-alert-db.csv"
)
PLANE_ALERT_IMAGES = (
    "https://raw.githubusercontent.com/sdr-enthusiasts/plane-alert-db/main/plane_images.csv"
)
VRS_AIRLINES = "https://raw.githubusercontent.com/vradarserver/standing-data/main/airlines/schema-01/airlines.csv"

OURAIRPORTS_AIRPORTS = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OURAIRPORTS_RUNWAYS = "https://davidmegginson.github.io/ourairports-data/runways.csv"
# Seaplane bases and balloonports don't help with aircraft arrivals.
AIRPORT_TYPES = {"large_airport", "medium_airport", "small_airport", "heliport", "closed"}

Importer = Callable[[Path, sqlite3.Connection], int]


def _clean(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return v or None


def import_aircraft(path: Path, conn: sqlite3.Connection) -> int:
    """tar1090-db aircraft.csv.gz: icao;reg;type;flags;desc;year;ownOp;"""
    conn.execute("DELETE FROM aircraft_db")
    rows = 0
    batch: list[tuple[str, str | None, str | None, str, str | None, str | None, str | None]] = []
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").split(";")
            if len(parts) < 7 or not parts[0]:
                continue
            batch.append(
                (
                    parts[0].strip().lower(),
                    _clean(parts[1]),
                    _clean(parts[2].upper()),
                    parts[3].strip(),
                    _clean(parts[4]),
                    _clean(parts[5]),
                    _clean(parts[6]),
                )
            )
            if len(batch) >= 20_000:
                conn.executemany("INSERT OR REPLACE INTO aircraft_db VALUES (?,?,?,?,?,?,?)", batch)
                rows += len(batch)
                batch.clear()
    conn.executemany("INSERT OR REPLACE INTO aircraft_db VALUES (?,?,?,?,?,?,?)", batch)
    rows += len(batch)
    conn.execute("DELETE FROM type_stats")
    conn.execute(
        "INSERT INTO type_stats(type_code, descr, airframes, military_share) "
        "SELECT type_code, max(descr), count(*), avg(substr(flags, 1, 1) = '1') "
        "FROM aircraft_db WHERE type_code IS NOT NULL GROUP BY type_code"
    )
    return rows


def import_ranges(path: Path, conn: sqlite3.Connection) -> int:
    data = json.loads(path.read_text())
    ranges = [(int(a, 16), int(b, 16)) for a, b in data.get("military", [])]
    conn.execute("DELETE FROM mil_ranges")
    conn.executemany("INSERT INTO mil_ranges VALUES (?, ?)", ranges)
    return len(ranges)


def _strip_header(h: str) -> str:
    return h.lstrip("$#").strip()


def import_plane_alert(path: Path, conn: sqlite3.Connection) -> int:
    with path.open(encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        header = [_strip_header(h) for h in next(reader)]
        idx = {h: i for i, h in enumerate(header)}

        def col(row: list[str], name: str) -> str | None:
            i = idx.get(name)
            return _clean(row[i]) if i is not None and i < len(row) else None

        out = []
        for row in reader:
            icao = col(row, "ICAO")
            if not icao:
                continue
            out.append(
                (
                    icao.lower(),
                    col(row, "Registration"),
                    col(row, "Operator"),
                    col(row, "Type"),
                    (col(row, "ICAO Type") or "").upper() or None,
                    col(row, "CMPG"),
                    col(row, "Tag 1"),
                    col(row, "Tag 2"),
                    col(row, "Tag 3"),
                    col(row, "Category"),
                    col(row, "Link"),
                )
            )
    conn.execute("DELETE FROM plane_alert")
    conn.executemany("INSERT OR REPLACE INTO plane_alert VALUES (?,?,?,?,?,?,?,?,?,?,?)", out)
    return len(out)


def import_plane_alert_images(path: Path, conn: sqlite3.Connection) -> int:
    with path.open(encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        next(reader)
        out = []
        for row in reader:
            if not row or not row[0].strip():
                continue
            links = [u.strip() for u in row[1:] if u.strip().startswith("https://")]
            if links:
                out.append((row[0].strip().lower(), json.dumps(links)))
    conn.execute("DELETE FROM plane_alert_images")
    conn.executemany("INSERT OR REPLACE INTO plane_alert_images VALUES (?, ?)", out)
    return len(out)


def import_airlines(path: Path, conn: sqlite3.Connection) -> int:
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        out = {}
        for row in reader:
            icao = _clean(row.get("ICAO"))
            name = _clean(row.get("Name"))
            if icao and name and len(icao) == 3:
                out[icao.upper()] = (icao.upper(), name, _clean(row.get("IATA")))
    conn.execute("DELETE FROM airlines")
    conn.executemany("INSERT INTO airlines VALUES (?, ?, ?)", list(out.values()))
    return len(out)


def _float(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def import_airports(path: Path, conn: sqlite3.Connection) -> int:
    out = []
    with path.open(encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.DictReader(f):
            kind = row.get("type") or ""
            lat, lon = _float(row.get("latitude_deg")), _float(row.get("longitude_deg"))
            if kind not in AIRPORT_TYPES or kind == "closed" or lat is None or lon is None:
                continue
            icao = _clean(row.get("icao_code")) or _clean(row.get("gps_code"))
            out.append(
                (
                    row["ident"].strip().upper(),
                    icao.upper() if icao else None,
                    (_clean(row.get("iata_code")) or "").upper() or None,
                    row.get("name") or row["ident"],
                    kind,
                    lat,
                    lon,
                    _float(row.get("elevation_ft")),
                    _clean(row.get("municipality")),
                    _clean(row.get("iso_country")),
                )
            )
    conn.execute("DELETE FROM airports")
    conn.executemany("INSERT OR REPLACE INTO airports VALUES (?,?,?,?,?,?,?,?,?,?)", out)
    return len(out)


def import_runways(path: Path, conn: sqlite3.Connection) -> int:
    out = []
    with path.open(encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("closed") == "1":
                continue
            out.append(
                (
                    (row.get("airport_ident") or "").upper(),
                    _clean(row.get("le_ident")),
                    _clean(row.get("he_ident")),
                    _float(row.get("length_ft")),
                    _float(row.get("le_latitude_deg")),
                    _float(row.get("le_longitude_deg")),
                    _float(row.get("le_heading_degT")),
                    _float(row.get("he_latitude_deg")),
                    _float(row.get("he_longitude_deg")),
                    _float(row.get("he_heading_degT")),
                )
            )
    conn.execute("DELETE FROM runways")
    conn.executemany("INSERT INTO runways VALUES (?,?,?,?,?,?,?,?,?,?)", out)
    return len(out)


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    url: str
    filename: str
    importer: Importer
    refresh_days: float = 7.0


SPECS = [
    DatasetSpec("tar1090-aircraft", TAR1090_AIRCRAFT, "aircraft.csv.gz", import_aircraft),
    DatasetSpec("tar1090-military-ranges", TAR1090_RANGES, "ranges.json", import_ranges),
    DatasetSpec("plane-alert-db", PLANE_ALERT, "plane-alert-db.csv", import_plane_alert),
    DatasetSpec(
        "plane-alert-images", PLANE_ALERT_IMAGES, "plane_images.csv", import_plane_alert_images
    ),
    DatasetSpec("vrs-airlines", VRS_AIRLINES, "airlines.csv", import_airlines, refresh_days=30),
    DatasetSpec("ourairports-airports", OURAIRPORTS_AIRPORTS, "airports.csv", import_airports, 30),
    DatasetSpec("ourairports-runways", OURAIRPORTS_RUNWAYS, "runways.csv", import_runways, 30),
]


class DatasetManager:
    def __init__(
        self,
        db: Database,
        data_dir: Path,
        http: httpx.AsyncClient,
        clock: Clock,
        specs: list[DatasetSpec] | None = None,
    ) -> None:
        self.db = db
        self.dir = data_dir / "datasets"
        self.http = http
        self.clock = clock
        self.specs = specs or SPECS
        self.busy: str | None = None

    async def status(self) -> list[dict[str, object]]:
        rows = {r["name"]: dict(r) for r in await self.db.fetchall("SELECT * FROM datasets")}
        return [
            {"name": s.name, "url": s.url, "refresh_days": s.refresh_days, **rows.get(s.name, {})}
            for s in self.specs
        ]

    async def _row(self, name: str) -> dict[str, object] | None:
        row = await self.db.fetchone("SELECT * FROM datasets WHERE name = ?", (name,))
        return dict(row) if row else None

    async def refresh_due(self, force: bool = False) -> list[str]:
        """Download and import every dataset that is missing or stale. Returns names imported."""
        imported = []
        for spec in self.specs:
            row = await self._row(spec.name)
            now = self.clock.now()
            checked = row.get("checked_at") if row else None
            fresh = isinstance(checked, float) and now - checked < spec.refresh_days * 86400
            if row and row.get("rows") and fresh and not force:
                continue
            try:
                if await self.refresh(spec, etag=None if force else _etag(row)):
                    imported.append(spec.name)
            except Exception as e:
                log.warning("dataset %s failed: %s", spec.name, e)
                await self._record(spec, error=str(e)[:300])
        return imported

    async def refresh(self, spec: DatasetSpec, etag: str | None = None) -> bool:
        self.busy = spec.name
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / spec.filename
            tmp = path.with_suffix(path.suffix + ".part")
            headers = {"If-None-Match": etag} if etag and path.exists() else {}
            async with self.http.stream("GET", spec.url, headers=headers, timeout=120) as resp:
                if resp.status_code == 304:
                    await self._record(spec)
                    return False
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                with tmp.open("wb") as f:
                    async for chunk in resp.aiter_bytes():
                        f.write(chunk)
                new_etag = resp.headers.get("etag")
            tmp.replace(path)
            rows = await asyncio.to_thread(self._import, spec, path)
            await self._record(spec, version=new_etag, rows=rows, imported=True)
            log.info("imported %s: %d rows", spec.name, rows)
            return True
        finally:
            self.busy = None

    def _import(self, spec: DatasetSpec, path: Path) -> int:
        conn = connect_sync(self.db.path)
        try:
            with conn:
                rows = spec.importer(path, conn)
            # Big imports leave a large WAL behind; fold it back into the main file.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return rows
        finally:
            conn.close()

    async def _record(
        self,
        spec: DatasetSpec,
        version: str | None = None,
        rows: int | None = None,
        imported: bool = False,
        error: str | None = None,
    ) -> None:
        now = self.clock.now()
        await self.db.execute(
            "INSERT INTO datasets(name, source_url, checked_at, last_error) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET checked_at = excluded.checked_at, "
            "last_error = excluded.last_error, source_url = excluded.source_url",
            (spec.name, spec.url, now, error),
        )
        if imported:
            await self.db.execute(
                "UPDATE datasets SET version = ?, rows = ?, imported_at = ? WHERE name = ?",
                (version, rows or 0, now, spec.name),
            )


def _etag(row: dict[str, object] | None) -> str | None:
    v = row.get("version") if row else None
    return v if isinstance(v, str) else None
