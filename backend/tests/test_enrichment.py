import gzip
import json
from pathlib import Path

import httpx
import respx

from app.classify.military import ClassifierSettings, Inputs, classify
from app.clock import FakeClock
from app.db import Database
from app.enrich import datasets as ds
from app.enrich.enricher import Enricher

AIRCRAFT_CSV = (
    "ADFEB7;98-0001;B752;10;BOEING C-32A;;;\n"
    "AE0000;63-8146;T38;10;NORTHROP T-38 Talon;;;\n"
    "A00001;N1;C680;0001;CESSNA 680;;FEDERAL AVIATION ADMINISTRATION;\n"
    "AC0B61;N87507;B738;00;BOEING 737-800;2012;UNITED AIRLINES INC;\n"
)
PLANE_ALERT_CSV = (
    "$ICAO,$Registration,$Operator,$Type,$ICAO Type,#CMPG,$Tag 1,$#Tag 2,$#Tag 3,Category,$#Link\n"
    "ADFEB7,98-0001,United States Air Force,Boeing C-32A,B752,Mil,Special Air Mission,VIP,Gov,"
    "USAF,https://example.org/c32\n"
)
IMAGES_CSV = "$ICAO,#ImageLink,#ImageLink2\nADFEB7,https://img.example/1.jpg,\n"
AIRLINES_CSV = "﻿Code,Name,ICAO,IATA\nUA,United Airlines,UAL,UA\nX,No Icao,,\n"
RANGES_JSON = {"military": [["adf7c8", "afffff"], ["43c000", "43cfff"]]}


@respx.mock
async def test_dataset_import_and_lookup(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    await db.connect()
    respx.get(ds.TAR1090_AIRCRAFT).mock(
        return_value=httpx.Response(
            200, content=gzip.compress(AIRCRAFT_CSV.encode()), headers={"etag": '"v1"'}
        )
    )
    respx.get(ds.TAR1090_RANGES).mock(return_value=httpx.Response(200, json=RANGES_JSON))
    respx.get(ds.PLANE_ALERT).mock(return_value=httpx.Response(200, text=PLANE_ALERT_CSV))
    respx.get(ds.PLANE_ALERT_IMAGES).mock(return_value=httpx.Response(200, text=IMAGES_CSV))
    respx.get(ds.VRS_AIRLINES).mock(return_value=httpx.Response(200, text=AIRLINES_CSV))
    clock = FakeClock()
    async with httpx.AsyncClient() as http:
        mgr = ds.DatasetManager(db, tmp_path, http, clock)
        imported = await mgr.refresh_due()
        assert len(imported) == 5
        assert await mgr.refresh_due() == []  # fresh, nothing to do
        # Stale with the same ETag: the server answers 304 and nothing is re-imported.
        clock.advance(8 * 86400)
        respx.get(ds.TAR1090_AIRCRAFT).mock(return_value=httpx.Response(304))
        assert "tar1090-aircraft" not in await mgr.refresh_due()

    enr = Enricher(db)
    await enr.reload()
    sam = await enr.lookup("ADFEB7")
    assert sam.registration == "98-0001" and sam.db_military
    assert sam.in_military_range
    assert sam.plane_alert is not None and sam.plane_alert.category == "USAF"
    assert sam.plane_alert.images == ("https://img.example/1.jpg",)
    faa = await enr.lookup("a00001")
    assert faa.db_ladd and not faa.db_military
    assert enr.type_info("T38") == (1, 1.0, "NORTHROP T-38 Talon")
    assert enr.airline_for("UAL1816") is not None
    assert enr.airline_for("N400XR") is None
    assert not enr.in_military_range("a00001")
    status = await mgr.status()
    assert {s["name"] for s in status if s.get("rows")} >= {"tar1090-aircraft", "plane-alert-db"}
    await db.close()


def _inputs(**kw: object) -> Inputs:
    base: dict[str, object] = dict(
        hex="abc123", callsign=None, registration=None, type_code=None, owner_op=None,
        db_military=False, in_military_range=False, plane_alert_cmpg=None,
        plane_alert_operator=None, type_airframes=0, type_military_share=None,
        callsign_is_airline=False, override=None,
    )  # fmt: skip
    base.update(kw)
    return Inputs(**base)  # type: ignore[arg-type]


def test_classifier_combines_signals_with_reasons() -> None:
    s = ClassifierSettings()
    c = classify(_inputs(db_military=True, in_military_range=True, callsign="SAM491"), s)
    assert c.military and c.military_score > 0.99
    assert {"db_flag", "mil_address", "callsign_prefix"} <= c.signals
    assert any("SAM" in r for r in c.reasons)


def test_kc46_dual_use_type_alone_is_not_military() -> None:
    s = ClassifierSettings()
    c = classify(_inputs(type_code="B762", type_airframes=262, type_military_share=0.53), s)
    assert not c.military and 0.2 < c.military_score < 0.3
    c2 = classify(_inputs(type_code="B762", type_airframes=262, type_military_share=0.53,
                          in_military_range=True), s)  # fmt: skip
    assert c2.military


def test_airline_callsigns_are_not_tactical() -> None:
    s = ClassifierSettings()
    assert classify(_inputs(callsign="UAL1816", callsign_is_airline=True), s).military_score == 0
    tactical = classify(_inputs(callsign="BOLT21"), s)
    assert "tactical_callsign" in tactical.signals and not tactical.military


def test_owner_keywords_word_boundaries_and_classes() -> None:
    s = ClassifierSettings()
    assert classify(_inputs(owner_op="UNITED STATES AIR FORCE"), s).military
    assert not classify(_inputs(owner_op="NAVYPIER HOLDINGS LLC"), s).military
    gov = classify(_inputs(owner_op="FEDERAL AVIATION ADMINISTRATION"), s)
    assert gov.government and not gov.military
    assert classify(_inputs(owner_op="KANSAS HIGHWAY PATROL"), s).police


def test_registration_pattern_and_override() -> None:
    s = ClassifierSettings()
    c = classify(_inputs(registration="05-5140"), s)
    assert "registration_pattern" in c.signals
    forced = classify(_inputs(registration="05-5140", override=False), s)
    assert not forced.military and forced.reasons[0] == "marked not military by you"


def test_classifier_settings_roundtrip() -> None:
    s = ClassifierSettings(threshold=0.5, callsign_prefixes=["HOOK"])
    again = ClassifierSettings.model_validate(json.loads(s.model_dump_json()))
    assert classify(_inputs(callsign="HOOK21"), again).military
