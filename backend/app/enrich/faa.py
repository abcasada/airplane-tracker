"""FAA Releasable Aircraft Database: the US civil register (public domain, refreshed daily).

The zip (~60 MB) holds MASTER.txt (one row per registered aircraft, keyed by N-number, with
the Mode S hex) and ACFTREF.txt (manufacturer/model by code), among others we don't read.
Both are comma-separated with space-padded values. They're streamed straight out of the zip
and inserted in batches, so the import's memory use stays small whatever the file size.
"""

import csv
import io
import re
import sqlite3
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"
FILENAME = "ReleasableAircraft.zip"
# MASTER.txt is ~200 MB uncompressed; anything far bigger isn't the file we expect.
MAX_UNCOMPRESSED_BYTES = 1_000_000_000
BATCH = 5_000
REF_SQL = "INSERT OR REPLACE INTO faa_aircraft_ref VALUES (?,?,?,?,?,?)"
MASTER_SQL = "INSERT OR REPLACE INTO faa_registry VALUES (?,?,?,?,?,?,?,?,?)"

# US civil Mode S addresses are derived from the N-number and sit in this block.
US_CIVIL_FIRST = 0xA00000
US_CIVIL_LAST = 0xADF7C7

REGISTRANT_TYPES: dict[str, tuple[str, str]] = {
    "1": ("individual", "Individual"),
    "2": ("partnership", "Partnership"),
    "3": ("corporation", "Corporation"),
    "4": ("co_owned", "Co-owned"),
    "5": ("government", "Government"),
    "7": ("llc", "LLC"),
    "8": ("non_citizen_corporation", "Non-citizen corporation"),
    "9": ("non_citizen_co_owned", "Non-citizen co-owned"),
}
AIRCRAFT_TYPES: dict[str, tuple[str, str]] = {
    "1": ("glider", "Glider"),
    "2": ("balloon", "Balloon"),
    "3": ("blimp", "Blimp / dirigible"),
    "4": ("fixed_wing_single", "Fixed wing, single engine"),
    "5": ("fixed_wing_multi", "Fixed wing, multi engine"),
    "6": ("rotorcraft", "Rotorcraft"),
    "7": ("weight_shift", "Weight-shift control"),
    "8": ("powered_parachute", "Powered parachute"),
    "9": ("gyroplane", "Gyroplane"),
    "H": ("hybrid_lift", "Hybrid lift"),
    "O": ("other", "Other"),
}
ENGINE_TYPES: dict[str, tuple[str, str]] = {
    "0": ("none", "None"),
    "1": ("reciprocating", "Reciprocating"),
    "2": ("turboprop", "Turboprop"),
    "3": ("turboshaft", "Turboshaft"),
    "4": ("turbojet", "Turbojet"),
    "5": ("turbofan", "Turbofan"),
    "6": ("ramjet", "Ramjet"),
    "7": ("two_cycle", "2-cycle"),
    "8": ("four_cycle", "4-cycle"),
    "9": ("unknown", "Unknown"),
    "10": ("electric", "Electric"),
    "11": ("rotary", "Rotary"),
}
_HEX = re.compile(r"^[0-9a-f]{6}$")
_SPACES = re.compile(r"\s+")


def is_us_civil(hex_: str) -> bool:
    try:
        return US_CIVIL_FIRST <= int(hex_, 16) <= US_CIVIL_LAST
    except ValueError:
        return False


def code_value(table: dict[str, tuple[str, str]], code: str | None) -> str | None:
    """FAA code → enum value; unknown non-empty codes become "other"."""
    if not code:
        return None
    hit = table.get(code.strip().upper())
    return hit[0] if hit else "other"


@dataclass(frozen=True, slots=True)
class FaaRecord:
    n_number: str
    registrant_type: str | None  # enum value, e.g. "government"
    name: str | None
    year: str | None
    status: str | None
    aircraft_type: str | None
    engine_type: str | None
    mfr: str | None
    model: str | None
    engines: int | None
    seats: int | None
    weight_class: str | None

    @property
    def type_desc(self) -> str | None:
        parts = [p for p in (self.mfr, self.model) if p]
        return " ".join(parts) or None


def _clean(v: str | None) -> str | None:
    if v is None:
        return None
    v = _SPACES.sub(" ", v).strip()
    return v or None


def _int(v: str | None) -> int | None:
    try:
        return int(v.strip()) if v and v.strip() else None
    except ValueError:
        return None


def _member(zf: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
    for info in zf.infolist():
        if info.filename.rsplit("/", 1)[-1].upper() == name:
            if info.file_size > MAX_UNCOMPRESSED_BYTES:
                raise ValueError(f"{name} is {info.file_size} bytes uncompressed; refusing")
            return info
    raise ValueError(f"{name} not found in the FAA zip")


def _rows(zf: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    """Rows as {HEADER: value}, streamed. Headers are uppercased and stripped (the files carry
    a BOM, padding and a trailing comma)."""
    with zf.open(_member(zf, name)) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
        reader = csv.reader(text)
        header = [h.strip().upper() for h in next(reader, [])]
        for row in reader:
            yield {h: v for h, v in zip(header, row, strict=False) if h}


def import_registry(path: Path, conn: sqlite3.Connection) -> int:
    """ACFTREF → faa_aircraft_ref, MASTER → faa_registry. Returns the number of aircraft."""
    with zipfile.ZipFile(path) as zf:
        conn.execute("DELETE FROM faa_aircraft_ref")
        refs: list[tuple[object, ...]] = []
        for r in _rows(zf, "ACFTREF.TXT"):
            code = _clean(r.get("CODE"))
            if not code:
                continue
            refs.append(
                (
                    code,
                    _clean(r.get("MFR")),
                    _clean(r.get("MODEL")),
                    _int(r.get("NO-ENG")),
                    _int(r.get("NO-SEATS")),
                    _clean(r.get("AC-WEIGHT")),
                )
            )
            if len(refs) >= BATCH:
                conn.executemany(
                    "INSERT OR REPLACE INTO faa_aircraft_ref VALUES (?,?,?,?,?,?)", refs
                )
                refs.clear()
        conn.executemany(REF_SQL, refs)

        conn.execute("DELETE FROM faa_registry")
        rows = 0
        batch: list[tuple[object, ...]] = []
        for r in _rows(zf, "MASTER.TXT"):
            hex_ = (r.get("MODE S CODE HEX") or "").strip().lower()
            n = _clean(r.get("N-NUMBER"))
            if not n or not _HEX.match(hex_):
                continue
            batch.append(
                (
                    hex_,
                    "N" + n.upper(),
                    _clean(r.get("MFR MDL CODE")),
                    _clean(r.get("YEAR MFR")),
                    _clean(r.get("TYPE REGISTRANT")),
                    _clean(r.get("NAME")),
                    _clean(r.get("STATUS CODE")),
                    _clean(r.get("TYPE AIRCRAFT")),
                    _clean(r.get("TYPE ENGINE")),
                )
            )
            if len(batch) >= BATCH:
                conn.executemany(
                    "INSERT OR REPLACE INTO faa_registry VALUES (?,?,?,?,?,?,?,?,?)", batch
                )
                rows += len(batch)
                batch.clear()
        conn.executemany(MASTER_SQL, batch)
        rows += len(batch)
    return rows


LOOKUP_SQL = (
    "SELECT r.*, a.mfr, a.model, a.engines, a.seats, a.weight_class FROM faa_registry r "
    "LEFT JOIN faa_aircraft_ref a ON a.code = r.mfr_mdl_code WHERE r.icao = ?"
)


def record_from_row(r: sqlite3.Row | dict[str, object]) -> FaaRecord:
    def s(k: str) -> str | None:
        v = r[k]
        return v if isinstance(v, str) and v else None

    def i(k: str) -> int | None:
        v = r[k]
        return v if isinstance(v, int) else None

    return FaaRecord(
        n_number=str(r["n_number"]),
        registrant_type=code_value(REGISTRANT_TYPES, s("registrant_type")),
        name=s("name"),
        year=s("year"),
        status=s("status"),
        aircraft_type=code_value(AIRCRAFT_TYPES, s("type_aircraft")),
        engine_type=code_value(ENGINE_TYPES, s("type_engine")),
        mfr=s("mfr"),
        model=s("model"),
        engines=i("engines"),
        seats=i("seats"),
        weight_class=s("weight_class"),
    )
