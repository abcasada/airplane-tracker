"""FAA Releasable Aircraft Database import and its use in enrichment, classification and filters.

The zip is hand-made in the FAA's format (BOM, space padding, trailing commas). Synthetic data.
"""

import io
import os
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from app.classify.military import ClassifierSettings, Inputs, classify
from app.clock import FakeClock
from app.db import Database
from app.enrich import datasets as ds
from app.enrich import faa
from app.enrich.enricher import Enricher
from app.filters.facts import EvalContext, LiveFacts
from app.filters.fields import FIELDS
from app.sources.base import Observation
from app.tracks.store import TrackStore
from app.watch import Watchlist

MASTER_HEADER = (
    "﻿N-NUMBER,SERIAL NUMBER,MFR MDL CODE,ENG MFR MDL,YEAR MFR,TYPE REGISTRANT,NAME,STREET,"
    "STREET2,CITY,STATE,ZIP CODE,REGION,COUNTY,COUNTRY,LAST ACTION DATE,CERT ISSUE DATE,"
    "CERTIFICATION,TYPE AIRCRAFT,TYPE ENGINE,STATUS CODE,MODE S CODE,FRACT OWNER,AIR WORTH DATE,"
    "OTHER NAMES(1),OTHER NAMES(2),OTHER NAMES(3),OTHER NAMES(4),OTHER NAMES(5),EXPIRATION DATE,"
    "UNIQUE ID,KIT MFR, KIT MODEL,MODE S CODE HEX,\r\n"
)


def master_row(
    n: str, mfr_code: str, year: str, reg_type: str, name: str, acft: str, eng: str, hex_: str
) -> str:
    cols = [
        n.ljust(5), "SYN-0001".ljust(30), mfr_code.ljust(7), "41514", year.ljust(4), reg_type,
        name.ljust(50), "1 EXAMPLE ST".ljust(33), "".ljust(33), "SPRINGFIELD".ljust(18), "KS",
        "67401".ljust(10), "4", "169", "US", "20240101", "20200101", "1N".ljust(10), acft,
        eng.rjust(2), "V ", "52000001", " ", "20000101", *[" " * 50] * 5, "20290101",
        "01234567", "".ljust(30), "".ljust(20), hex_.upper().ljust(10),
    ]  # fmt: skip
    return ",".join(cols) + ",\r\n"


ACFTREF = (
    "﻿CODE,MFR,MODEL,TYPE-ACFT,TYPE-ENG,AC-CAT,BUILD-CERT-IND,NO-ENG,NO-SEATS,AC-WEIGHT,"
    "SPEED,TC-DATA-SHEET,TC-DATA-HOLDER,\r\n"
    "2072738,CESSNA                        ,172S                ,4,1 ,1,0,01,004,CLASS 1,0000,"
    "3A12      ,TEXTRON AVIATION INC,\r\n"
    "7100510,BELL                          ,407                 ,6,3 ,1,0,01,007,CLASS 1,0000,"
    "H2SW      ,BELL TEXTRON INC,\r\n"
)
# Hexes must sit in the US civil block a00000–adf7c7.
MASTER = (
    MASTER_HEADER
    + master_row("12345", "2072738", "2004", "1", "DOE JOHN Q", "4", "1", "a061d9")
    + master_row("407SP", "7100510", "2015", "5", "STATE OF KANSAS", "6", "3", "a4f2b3")
    + master_row("1", "2072738", "1999", "5", "FEDERAL AVIATION ADMINISTRATION", "4", "1", "a00001")
    + master_row("99999", "9999999", "", "3", "NO REF CORP", "O", "", "")  # no hex: skipped
)


def make_zip(master: str = MASTER, acftref: str = ACFTREF) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("MASTER.txt", master)
        zf.writestr("ACFTREF.txt", acftref)
        zf.writestr("DEREG.txt", "N-NUMBER,\r\n")  # present in the real file, ignored
    return buf.getvalue()


AIRCRAFT_CSV = "A00001;N1;C680;0001;CESSNA 680;;FEDERAL AVIATION ADMINISTRATION;\n"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    d = Database(tmp_path / "db.sqlite3")
    await d.connect()
    yield d
    await d.close()


@respx.mock
async def test_import_lookup_and_conditional_refresh(tmp_path: Path, db: Database) -> None:
    import gzip

    respx.get(ds.TAR1090_AIRCRAFT).mock(
        return_value=httpx.Response(200, content=gzip.compress(AIRCRAFT_CSV.encode()))
    )
    lm = "Mon, 16 Nov 2026 05:00:00 GMT"
    route = respx.get(faa.URL).mock(
        return_value=httpx.Response(200, content=make_zip(), headers={"last-modified": lm})
    )
    clock = FakeClock()
    async with httpx.AsyncClient() as http:
        specs = [s for s in ds.SPECS if s.name in ("faa-registry", "tar1090-aircraft")]
        mgr = ds.DatasetManager(db, tmp_path, http, clock, specs)
        assert set(await mgr.refresh_due()) == {"faa-registry", "tar1090-aircraft"}
        status = {s["name"]: s for s in await mgr.status()}
        assert status["faa-registry"]["rows"] == 3
        # A week later the FAA file is checked with If-Modified-Since; 304 means no re-import.
        clock.advance(8 * 86400)
        route.mock(return_value=httpx.Response(304))
        assert "faa-registry" not in await mgr.refresh_due()
        assert route.calls.last.request.headers["if-modified-since"] == lm

    enr = Enricher(db, clock)
    await enr.reload()
    # Not in tar1090-db: everything comes from the FAA, joined with ACFTREF.
    a = await enr.lookup("A061D9")
    assert a.registration == "N12345" and a.owner_op == "DOE JOHN Q" and a.year == "2004"
    assert a.type_desc == "CESSNA 172S" and a.type_code is None
    assert a.provenance == {"registration": "faa", "type_desc": "faa", "owner_op": "faa",
                            "year": "faa"}  # fmt: skip
    assert a.faa is not None and a.faa.registrant_type == "individual"
    assert a.faa.aircraft_type == "fixed_wing_single" and a.faa.engine_type == "reciprocating"
    assert a.faa.seats == 4 and a.faa.weight_class == "CLASS 1"
    heli = await enr.lookup("a4f2b3")
    assert heli.faa is not None and heli.faa.registrant_type == "government"
    assert heli.faa.aircraft_type == "rotorcraft" and heli.faa.engine_type == "turboshaft"
    # tar1090-db values win; the FAA only fills what's missing (here: the year).
    n1 = await enr.lookup("a00001")
    assert n1.registration == "N1" and n1.type_desc == "CESSNA 680" and n1.type_code == "C680"
    assert n1.year == "1999" and n1.provenance == {"year": "faa"}
    # Outside the US civil block the registry isn't consulted.
    assert (await enr.lookup("ae1234")).faa is None


def test_us_civil_block() -> None:
    assert faa.is_us_civil("a00001") and faa.is_us_civil("adf7c7")
    assert not faa.is_us_civil("adf7c8") and not faa.is_us_civil("4010ee")
    assert not faa.is_us_civil("~a00001")


def test_importer_is_streaming_and_guards_size(tmp_path: Path) -> None:
    import sqlite3

    from app.db import _migrations

    path = tmp_path / faa.FILENAME
    path.write_bytes(make_zip())
    conn = sqlite3.connect(":memory:")
    for _, _, sql in _migrations():
        conn.executescript(sql)
    assert faa.import_registry(path, conn) == 3
    # Re-import replaces, never duplicates.
    assert faa.import_registry(path, conn) == 3
    assert conn.execute("SELECT count(*) FROM faa_registry").fetchone()[0] == 3
    row = conn.execute("SELECT n_number, name FROM faa_registry WHERE icao='a4f2b3'").fetchone()
    assert row == ("N407SP", "STATE OF KANSAS")
    # Street addresses are never stored.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(faa_registry)")]
    assert not any("street" in c.lower() or "zip" in c.lower() for c in cols)

    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("ACFTREF.txt", ACFTREF)
    with pytest.raises(ValueError, match="MASTER"):
        faa.import_registry(bad, conn)
    old = faa.MAX_UNCOMPRESSED_BYTES
    faa.MAX_UNCOMPRESSED_BYTES = 100
    try:
        with pytest.raises(ValueError, match="refusing"):
            faa.import_registry(path, conn)
    finally:
        faa.MAX_UNCOMPRESSED_BYTES = old


@respx.mock
async def test_manual_copy_is_imported_when_download_is_refused(
    tmp_path: Path, db: Database
) -> None:
    respx.get(faa.URL).mock(return_value=httpx.Response(503))
    (tmp_path / "datasets").mkdir()
    local = tmp_path / "datasets" / faa.FILENAME
    local.write_bytes(make_zip())
    clock = FakeClock(start=1_000_000_000.0)  # the file's mtime is newer than any import
    async with httpx.AsyncClient() as http:
        specs = [s for s in ds.SPECS if s.name == "faa-registry"]
        mgr = ds.DatasetManager(db, tmp_path, http, clock, specs)
        assert await mgr.refresh_due() == ["faa-registry"]
        st = (await mgr.status())[0]
        assert st["rows"] == 3 and "local file" in str(st["last_error"])
        # Same file, later failure: nothing to re-import.
        clock.advance(8 * 86400)
        os.utime(local, (900_000_000, 900_000_000))
        assert await mgr.refresh_due() == []


def _inputs(**kw: object) -> Inputs:
    base: dict[str, object] = dict(
        hex="a4f2b3", callsign=None, registration="N407SP", type_code="B407", owner_op=None,
        db_military=False, in_military_range=False, plane_alert_cmpg=None,
        plane_alert_operator=None, type_airframes=0, type_military_share=None,
        callsign_is_airline=False, override=None,
    )  # fmt: skip
    base.update(kw)
    return Inputs(**base)  # type: ignore[arg-type]


def test_government_registrant_marks_government_not_military() -> None:
    s = ClassifierSettings()
    c = classify(_inputs(registrant_type="government"), s)
    assert c.government and not c.military and c.military_score == 0
    assert not classify(_inputs(registrant_type="corporation"), s).government
    assert not classify(_inputs(), s).government
    off = ClassifierSettings(government_registrant_types=[])
    assert not classify(_inputs(registrant_type="government"), off).government


def test_faa_filter_fields() -> None:
    from app.enrich.models import AircraftInfo

    for key in ("registrant_type", "faa_aircraft_type", "faa_engine_type"):
        fd = FIELDS[key]
        assert fd.type == "enum" and fd.help and fd.enum
        assert len({v for v, _ in fd.enum}) == len(fd.enum)
    rec = faa.FaaRecord("N407SP", "government", "STATE OF KANSAS", "2015", "V", "rotorcraft",
                        "turboshaft", "BELL", "407", 1, 7, "CLASS 1")  # fmt: skip
    store = TrackStore()
    t, _ = store.update(Observation(hex="a4f2b3", source="test", seen_at=0.0))
    t.info = AircraftInfo(faa=rec)
    f = LiveFacts(t, EvalContext(0.0, {}, ZoneInfo("UTC")))
    assert f.get("registrant_type") == "government"
    assert f.get("faa_aircraft_type") == "rotorcraft"
    assert f.get("faa_engine_type") == "turboshaft"
    t2, _ = store.update(Observation(hex="4010ee", source="test", seen_at=0.0))
    t2.info = AircraftInfo()
    assert LiveFacts(t2, EvalContext(0.0, {}, ZoneInfo("UTC"))).get("registrant_type") is None


def test_unknown_codes_become_other() -> None:
    assert faa.code_value(faa.REGISTRANT_TYPES, "T") == "other"
    assert faa.code_value(faa.ENGINE_TYPES, "10") == "electric"
    assert faa.code_value(faa.AIRCRAFT_TYPES, None) is None


async def test_watch_resolves_n_numbers_from_the_registry(db: Database) -> None:
    from app.filters.models import Condition, Group
    from tests.test_watch import flt

    await db.execute(
        "INSERT INTO faa_registry(icao, n_number) VALUES ('a4f2b3', 'N407SP')",
    )
    f = flt(1, Group(children=[Condition(field="registration", op="in", value=["N407SP"])]))
    w = Watchlist(db)
    await w.refresh([f])
    assert w.hexes() == ["a4f2b3"]
