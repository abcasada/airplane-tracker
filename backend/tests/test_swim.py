"""FAA SWIM (SCDS TFMS) schedule source: parsing, filtering, storage, fields and the bridge."""

import gzip
from datetime import UTC, datetime
from pathlib import Path

from app.sources.swim import (
    airport_aliases,
    decode_payload,
    interest_pattern,
    normalize_acid,
    parse_payload,
    parse_time,
)

FIXTURES = Path(__file__).parent / "fixtures" / "swim"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def ts(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()


# ---- parsing ------------------------------------------------------------------------------


def test_parses_a_flight_plan_with_prefixed_namespaces() -> None:
    r = parse_payload(fixture("flight_plan.xml"))
    assert r.messages == 2 and r.errors == 0 and r.skipped == 0
    u = r.updates[0]
    assert u.key == "ref:100001"
    assert u.acid == "TST101"
    assert (u.origin, u.destination) == ("KDEN", "KICT")
    assert u.gufi == "TST0000001"
    assert u.aircraft_type == "B763"  # from "H/B763/L"
    assert u.scheduled_departure == ts("2027-01-15T14:00:00")
    assert (u.etd, u.etd_type) == (ts("2027-01-15T14:10:00"), "PROPOSED")
    assert (u.eta, u.eta_type) == (ts("2027-01-15T15:25:00"), "ESTIMATED")
    assert u.sent_at == ts("2027-01-15T12:00:00")
    assert not u.cancelled and u.departed_at is None
    assert r.updates[1].aircraft_type == "A320"


def test_parses_default_namespace_faa_codes_and_status() -> None:
    r = parse_payload(fixture("modify_default_ns.xml"))
    u = r.updates[0]
    assert (u.origin, u.destination) == ("DEN", "ICT")
    assert u.status == "SCHEDULED"
    assert u.aircraft_type == "B763"
    assert u.msg_type == "FlightModify"
    assert u.sent_at == ts("2027-01-15T13:30:00.250")
    assert (u.etd, u.eta) == (ts("2027-01-15T14:20:00"), ts("2027-01-15T15:35:00"))


def test_parses_departure_arrival_and_cancellation() -> None:
    r = parse_payload(fixture("departure_arrival.xml"))
    dep, cancel, arr = r.updates
    assert dep.departed_at == ts("2027-01-15T14:21:00") and dep.etd_type == "ACTUAL"
    assert cancel.cancelled and cancel.acid == "N123TS"
    assert arr.arrived_at == ts("2027-01-15T15:32:00")


def test_flow_information_and_junk_are_ignored() -> None:
    r = parse_payload(fixture("flow_info.xml"))
    assert r.updates == [] and r.messages == 0 and r.errors == 0
    assert parse_payload(b"<not xml").errors == 1
    assert parse_payload(b"").errors == 1
    assert parse_payload(None).errors == 1


def test_gzip_payloads_are_inflated_and_oversized_dropped() -> None:
    raw = fixture("flight_plan.xml")
    assert decode_payload(gzip.compress(raw)) == raw
    assert decode_payload("<a/>") == b"<a/>"
    assert decode_payload(gzip.compress(b" " * 3_000_000)) is None
    assert len(parse_payload(gzip.compress(raw)).updates) == 2


def test_wanted_skips_message_bodies() -> None:
    seen: list[tuple[str | None, str | None, str]] = []

    def wanted(dep: str | None, arr: str | None, acid: str) -> bool:
        seen.append((dep, arr, acid))
        return "KICT" in (dep, arr)

    r = parse_payload(fixture("flight_plan.xml"), wanted)
    assert [u.acid for u in r.updates] == ["TST101"]
    assert r.skipped == 1
    assert seen == [("KDEN", "KICT", "TST101"), ("KBOS", "KJFK", "OTH200")]


def test_interest_pattern_matches_headers_by_icao_or_faa_code() -> None:
    p = interest_pattern({"KICT"}, set())
    assert p is not None
    assert p.search(fixture("flight_plan.xml"))
    assert p.search(fixture("modify_default_ns.xml"))  # arrArpt="ICT"
    assert not p.search(fixture("flow_info.xml"))
    by_reg = interest_pattern(set(), {"N123TS"})
    assert by_reg is not None and by_reg.search(fixture("departure_arrival.xml"))
    assert interest_pattern(set(), set()) is None


def test_small_helpers() -> None:
    assert airport_aliases("KICT") == {"KICT", "ICT"}
    assert airport_aliases("PANC") == {"PANC", "ANC"}
    assert airport_aliases("EGLL") == {"EGLL"}
    assert normalize_acid(" n-123ts ") == "N123TS"
    assert normalize_acid("bad!") is None
    assert parse_time("2027-01-15T14:00:00Z") == ts("2027-01-15T14:00:00")
    assert parse_time("2027-01-15T14:00:00") is None  # no zone: refuse rather than guess
    assert parse_time("soon") is None
