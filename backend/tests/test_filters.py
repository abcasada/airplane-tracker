from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.classify.military import ClassifierSettings, Inputs, classify
from app.enrich.models import AircraftInfo, PlaneAlertEntry
from app.filters.engine import CompiledFilter, describe
from app.filters.facts import EvalContext, LiveFacts, SnapshotFacts
from app.filters.fields import FIELDS, catalog
from app.filters.models import Condition, FilterDefinition, Group
from app.places.models import Place, Visibility
from app.sources.base import Observation
from app.tracks.store import TrackStore

HOME = Place(id=1, name="Home", lat=38.84, lon=-97.61, elevation_ft=1220, radius_nm=10,
             visibility=Visibility(), is_home=True, created_at=0, updated_at=0)  # fmt: skip
RANGE = Place(
    id=2, name="Range", role="area", lat=38.67, lon=-97.85, radius_nm=10, created_at=0,
    updated_at=0, alt_ceiling_ft=23000,
    polygon=[(38.7556, -97.7669), (38.6625, -97.7669), (38.6389, -97.7919), (38.6389, -97.8336),
             (38.5833, -97.8336), (38.5833, -97.9336), (38.7556, -97.9336)],
)  # fmt: skip
PLACES = {1: HOME, 2: RANGE}
NOW = 1_800_000_000.0


def make_facts(**kw: Any) -> LiveFacts:
    defaults: dict[str, Any] = dict(
        hex="ae1234", source="test", seen_at=NOW, pos_time=NOW, lat=38.86, lon=-97.60,
        alt_baro_ft=4220, gs_kt=250, track_deg=180, callsign="RCH452", squawk="1234",
        category="A5", type_code="C17", registration="05-5140",
    )  # fmt: skip
    info_kw = kw.pop("info", {})
    defaults.update(kw)
    store = TrackStore()
    track, _ = store.update(Observation(**defaults))
    track.info = AircraftInfo(**info_kw)
    track.classification = classify(
        Inputs(
            hex=track.hex, callsign=track.callsign, registration=track.registration,
            type_code=track.type_code, owner_op=track.owner_op,
            db_military=track.info.db_military, in_military_range=track.info.in_military_range,
            plane_alert_cmpg=track.info.plane_alert.cmpg if track.info.plane_alert else None,
            plane_alert_operator=None, type_airframes=277,
            type_military_share=1.0 if track.type_code == "C17" else 0.0,
            callsign_is_airline=False, override=None,
        ),
        ClassifierSettings(),
    )  # fmt: skip
    return LiveFacts(track, EvalContext(NOW, PLACES, ZoneInfo("America/Chicago")))


def cond(field: str, op: str, value: Any = None, place: int | None = None, **kw: Any) -> Condition:
    return Condition(field=field, op=op, value=value, place=place, **kw)


def flt(*children: Condition | Group, mode: str = "all") -> CompiledFilter:
    return CompiledFilter(
        FilterDefinition(root=Group(mode=mode, children=list(children))),
        PLACES,
    )


def test_catalog_is_complete() -> None:
    cat = catalog()
    assert {c["key"] for c in cat} == set(FIELDS)
    for c in cat:
        assert c["help"] and c["ops"], c["key"]


def test_military_visible_from_home() -> None:
    f = make_facts()
    assert f.get("distance_nm", 1) == pytest.approx(1.3, abs=0.1)
    assert f.get("height_above_place_ft", 1) == 3000
    assert f.get("visible_now", 1) is True
    assert f.get("direction_from_place", 1) == "N"
    flt_ = flt(cond("military", "is_true"), cond("visible_now", "is_true", place=1))
    assert flt_.matches(f)


def test_low_on_horizon_is_not_visible() -> None:
    far = make_facts(lat=39.10, alt_baro_ft=3220)  # ~15.6 nm away, 2,000 ft up: ~1.2°
    assert far.get("elevation_angle_deg", 1) < 5
    assert far.get("visible_now", 1) is False


def test_inside_area_and_distance_to_polygon() -> None:
    inside = make_facts(lat=38.70, lon=-97.85, alt_baro_ft=15000)
    assert inside.get("inside_area", 2) is True
    assert inside.get("distance_nm", 2) == 0
    above = make_facts(lat=38.70, lon=-97.85, alt_baro_ft=30000)
    assert above.get("inside_area", 2) is False
    outside = make_facts(lat=38.70, lon=-97.70)
    assert outside.get("inside_area", 2) is False
    assert outside.get("distance_nm", 2) == pytest.approx(3.1, abs=0.3)


@pytest.mark.parametrize(
    ("c", "expected"),
    [
        (cond("type_code", "in", ["c17", "K35R"]), True),
        (cond("type_code", "not_in", ["C17"]), False),
        (cond("type_code", "glob", ["C1*"]), True),
        (cond("callsign", "regex", r"^(RCH|REACH)\d+"), True),
        (cond("callsign", "contains", ["ch4"]), True),
        (cond("altitude_ft", "between", [3000, 5000]), True),
        (cond("altitude_ft", "lt", 1000), False),
        (cond("operator", "not_in", ["Acme"]), True),  # unknown operator is "not Acme"
        (cond("operator", "in", ["Acme"]), False),
        (cond("operator", "exists"), False),
        (cond("military_signals", "contains_any", ["callsign_prefix"]), True),
        (cond("military_signals", "contains_none", ["db_flag"]), True),
        (cond("local_hour", "between", [22, 6]), True),  # NOW is Fri 02:00 Chicago time
        (cond("local_hour", "between", [6, 22]), False),
        (cond("day_of_week", "in", ["Fri"]), True),
    ],
)
def test_operators(c: Condition, expected: bool) -> None:
    assert flt(c).matches(make_facts()) is expected


def test_groups_and_disabled_conditions() -> None:
    f = make_facts()
    any_group = Group(
        mode="any", children=[cond("type_code", "in", ["B738"]), cond("military", "is_true")]
    )
    none_group = Group(mode="none", children=[cond("on_ground", "is_true")])
    assert flt(any_group, none_group).matches(f)
    off = cond("type_code", "in", ["B738"], enabled=False)
    assert flt(off, cond("military", "is_true")).matches(f)
    assert not flt(off).matches(f)  # nothing enabled: never matches
    assert not flt().matches(f)


def test_invalid_filters_report_errors_and_never_match() -> None:
    bad = flt(cond("distance_nm", "lt", 5), cond("callsign", "regex", "(unclosed"),
              cond("visible_now", "is_true", place=2), cond("nope", "eq", 1))  # fmt: skip
    assert not bad.valid
    text = " ".join(bad.errors)
    assert "needs a place" in text and "invalid pattern" in text
    assert "only works with a viewpoint" in text and "Unknown field" in text
    assert not bad.matches(make_facts())


def test_funnel_explain_and_snapshot_replay() -> None:
    facts = [make_facts(), make_facts(hex="a1", type_code="B738", category="A3", callsign="UAL1",
                                      registration="N1")]  # fmt: skip
    f = flt(cond("type_code", "in", ["C17", "B738"], id="a"), cond("military", "is_true", id="b"),
            cond("visible_now", "is_true", place=1, id="c"))  # fmt: skip
    steps = f.funnel(facts)  # type: ignore[arg-type]
    assert [s["count"] for s in steps] == [2, 2, 1, 1]
    ex = f.explain(facts[1])
    assert ex["result"] is False
    assert [c["result"] for c in ex["children"]] == [True, False, True]
    snap = facts[0].snapshot(f.referenced())
    assert f.matches(SnapshotFacts("ae1234", snap))


def test_plane_alert_fields() -> None:
    pa = PlaneAlertEntry(
        "ae1234", "05-5140", "USAF", "C-17", "C17", "Mil", ("Airlift",), "USAF", None
    )
    f = make_facts(info={"plane_alert": pa})
    assert flt(cond("plane_alert_category", "in", ["usaf"])).matches(f)
    assert flt(cond("plane_alert_tags", "contains_any", ["AIRLIFT"])).matches(f)


def test_describe() -> None:
    d = FilterDefinition(root=Group(children=[
        cond("military", "is_true"),
        Group(mode="any", children=[cond("visible_now", "is_true", place=1),
                                    cond("inside_area", "is_true", place=2)]),
        cond("altitude_ft", "lt", 10000),
    ]))  # fmt: skip
    assert describe(d, PLACES) == (
        "Military AND (Visible now (from Home) OR Inside area (Range)) "
        "AND Altitude (MSL) < 10000 ft"
    )


def test_operator_merges_duplicate_names() -> None:
    from app.enrich.models import Airline

    f = make_facts(callsign="SWA1739", info={"owner_op": "SOUTHWEST AIRLINES CO"})
    f.track.airline = Airline("SWA", "Southwest Airlines", None, None)
    assert f.get("operator") == "Southwest Airlines"
    g = make_facts(callsign="UAL1", info={"owner_op": "UMB BANK NA TRUSTEE"})
    g.track.airline = Airline("UAL", "United Airlines", None, None)
    assert g.get("operator") == "United Airlines / UMB BANK NA TRUSTEE"
