"""Military review suggestions derived from your overrides."""

import json
from typing import Any

from app.classify.military import ClassifierSettings, Inputs, classify
from app.classify.suggest import (
    MIN_EVIDENCE,
    Mark,
    Suggestion,
    SuggestionPrefs,
    apply,
    from_snapshot,
    suggest,
    to_snapshot,
    visible,
)
from app.sources.base import Observation, SourceBatch
from tests.conftest import Harness
from tests.test_enrichment import _inputs
from tests.test_pipeline import HOME, c17


def _mark(key: str, mil: bool, **kw: object) -> Mark:
    return Mark(key, mil, _inputs(hex=key, **kw))


def _by_id(items: list[Suggestion]) -> dict[str, Suggestion]:
    return {s.id: s for s in items}


def test_callsign_prefix_needs_three_agreeing_marks() -> None:
    s = ClassifierSettings()
    marks = [_mark(f"a{i}", True, callsign=f"BOLT{i}1") for i in range(MIN_EVIDENCE)]
    got = _by_id(suggest(marks, s))
    add = got["callsign_prefix:add:BOLT"]
    assert add.count == 3 and add.kind == "add_callsign_prefix"
    assert "3 of them score below the threshold" in add.reason
    assert sorted(add.evidence) == ["BOLT01", "BOLT11", "BOLT21"]

    assert "callsign_prefix:add:BOLT" not in _by_id(suggest(marks[:2], s))
    contradicted = [*marks, _mark("b1", False, callsign="BOLT55")]
    assert "callsign_prefix:add:BOLT" not in _by_id(suggest(contradicted, s))

    after = apply(s, add)
    assert "BOLT" in after.callsign_prefixes
    assert classify(_inputs(callsign="BOLT77"), after).signals >= {"callsign_prefix"}
    assert "callsign_prefix:add:BOLT" not in _by_id(suggest(marks, after))


def test_callsigns_equal_to_registration_or_already_listed_are_skipped() -> None:
    s = ClassifierSettings()
    same = [_mark(f"a{i}", True, callsign=f"ZZ33{i}", registration=f"ZZ33{i}") for i in range(3)]
    listed = [_mark(f"b{i}", True, callsign=f"RCH{i}12") for i in range(3)]
    ids = _by_id(suggest(same + listed, s))
    assert not any(i.startswith("callsign_prefix:add") for i in ids)


def test_tactical_misses_suggest_raising_that_weight() -> None:
    s = ClassifierSettings()
    marks = [_mark(f"a{i}", True, callsign=f"BOLT{i}1") for i in range(3)]
    raise_ = _by_id(suggest(marks, s))["weight:tactical_callsign:raise"]
    # 0.6 is the first slider step at which the clue alone reaches the 0.6 threshold.
    assert raise_.value == 0.6 and raise_.current == 0.3 and raise_.count == 3
    assert apply(s, raise_).weights["tactical_callsign"] == 0.6

    # A tactical-looking callsign you marked not military would flip too: no suggestion.
    other = [*marks, _mark("b1", False, callsign="HOOK21")]
    assert "weight:tactical_callsign:raise" not in _by_id(suggest(other, s))


def test_prefix_and_owner_word_removals() -> None:
    s = ClassifierSettings()
    marks = [
        *(_mark(f"a{i}", False, callsign=f"PAT{i}0") for i in range(3)),
        *(_mark(f"b{i}", False, owner_op="SALVATION ARMY TRUST") for i in range(3)),
    ]
    got = _by_id(suggest(marks, s))
    assert got["callsign_prefix:remove:PAT"].count == 3
    assert got["owner_keyword:remove:ARMY"].count == 3
    after = apply(apply(s, got["callsign_prefix:remove:PAT"]), got["owner_keyword:remove:ARMY"])
    assert "PAT" not in after.callsign_prefixes and "ARMY" not in after.owner_keywords

    # One military mark with the same word keeps it.
    kept = [*marks, _mark("c1", True, owner_op="UNITED STATES ARMY")]
    assert "owner_keyword:remove:ARMY" not in _by_id(suggest(kept, s))


def test_owner_additions() -> None:
    s = ClassifierSettings()
    marks = [_mark(f"a{i}", True, owner_op="Fuerza  Aerea Example ") for i in range(3)]
    got = _by_id(suggest(marks, s))
    add = got["owner_keyword:add:FUERZA AEREA EXAMPLE"]
    assert add.count == 3
    assert classify(_inputs(owner_op="FUERZA AEREA EXAMPLE"), apply(s, add)).military

    covered = [_mark(f"b{i}", True, owner_op="ROYAL EXAMPLE AIR FORCE") for i in range(3)]
    assert not any(i.startswith("owner_keyword:add") for i in _by_id(suggest(covered, s)))
    contradicted = [*marks, _mark("c1", False, owner_op="FUERZA AEREA EXAMPLE")]
    assert "owner_keyword:add:FUERZA AEREA EXAMPLE" not in _by_id(suggest(contradicted, s))


def test_lowering_a_weight_stops_before_breaking_other_marks() -> None:
    s = ClassifierSettings()
    wrong = [_mark(f"a{i}", False, in_military_range=True) for i in range(3)]
    sure = _mark("b1", True, in_military_range=True, db_military=True)
    lower = _by_id(suggest([*wrong, sure], s))["weight:mil_address:lower"]
    # The largest slider step below the 0.6 threshold.
    assert lower.value == 0.55 and lower.count == 3
    assert "none of your other marks change" in lower.reason

    # A mark that's military only because of this clue would drop: no suggestion.
    only = _mark("c1", True, in_military_range=True)
    assert "weight:mil_address:lower" not in _by_id(suggest([*wrong, sure, only], s))


def test_dual_use_weight_and_dismissed() -> None:
    s = ClassifierSettings()

    def h60s(share: float) -> list[Mark]:
        return [
            _mark(f"a{i}", True, type_code="H60", type_airframes=100, type_military_share=share)
            for i in range(3)
        ]

    # 60% military: even the top weight can't reach the threshold, so nothing to suggest.
    assert not suggest(h60s(0.6), s)
    marks = h60s(0.8)
    sug = _by_id(suggest(marks, s))["weight:dual_use_type:raise"]
    assert sug.value == 0.75 and apply(s, sug).dual_use_max_weight == 0.75
    prefs = SuggestionPrefs()
    prefs.dismiss("weight:dual_use_type:raise", sug.count)
    assert "weight:dual_use_type:raise" not in _by_id(visible(suggest(marks, s, prefs), prefs))


def test_min_evidence_setting() -> None:
    s = ClassifierSettings()
    two = [_mark(f"a{i}", True, callsign=f"BOLT{i}1") for i in range(2)]
    assert "callsign_prefix:add:BOLT" not in _by_id(suggest(two, s))
    assert "callsign_prefix:add:BOLT" in _by_id(suggest(two, s, SuggestionPrefs(min_evidence=2)))
    four = [_mark(f"a{i}", True, callsign=f"BOLT{i}1") for i in range(4)]
    assert "callsign_prefix:add:BOLT" not in _by_id(
        suggest(four, s, SuggestionPrefs(min_evidence=5))
    )


def test_dismissed_comes_back_when_evidence_doubles_only_if_enabled() -> None:
    s = ClassifierSettings()
    sid = "callsign_prefix:add:BOLT"

    def marks(n: int) -> list[Mark]:
        return [_mark(f"a{i}", True, callsign=f"BOLT{i}1") for i in range(n)]

    prefs = SuggestionPrefs()
    prefs.dismiss(sid, 3)
    for n in (3, 5, 6, 9):  # off by default: never comes back
        assert sid not in _by_id(visible(suggest(marks(n), s, prefs), prefs))
    prefs.resurface_on_double = True
    assert sid not in _by_id(visible(suggest(marks(5), s, prefs), prefs))
    back = _by_id(visible(suggest(marks(6), s, prefs), prefs))[sid]
    assert back.count == 6 and back.dismissed_count == 3
    # Dismissing it again raises the bar to double the new count.
    prefs.dismiss(sid, 6)
    assert prefs.dismissed == [sid] and prefs.dismissed_counts == {sid: 6}
    assert sid not in _by_id(visible(suggest(marks(11), s, prefs), prefs))
    assert sid in _by_id(visible(suggest(marks(12), s, prefs), prefs))
    # Dismissals saved before counts were kept never come back on their own.
    old = SuggestionPrefs(dismissed=[sid], resurface_on_double=True)
    assert sid not in _by_id(visible(suggest(marks(12), s, old), old))


def test_overrides_inside_inputs_are_ignored() -> None:
    s = ClassifierSettings()
    marks = [Mark(f"a{i}", True, _inputs(callsign=f"BOLT{i}1", override=True)) for i in range(3)]
    assert "callsign_prefix:add:BOLT" in _by_id(suggest(marks, s))


def test_snapshot_roundtrip_refreshes_type_stats() -> None:
    i = _inputs(callsign="BOLT11", owner_op="X", type_code="H60", type_airframes=5,
                type_military_share=0.1, db_military=True, override=True)  # fmt: skip
    snap = to_snapshot(i)
    assert "override" not in snap and "type_airframes" not in snap
    back = from_snapshot(json.loads(json.dumps(snap)), (100, 0.6))
    assert back.callsign == "BOLT11" and back.db_military and back.override is None
    assert back.type_airframes == 100 and back.type_military_share == 0.6
    assert isinstance(back, Inputs)


# ---- API -------------------------------------------------------------------------------------


def _bolt(h: Harness, n: int, callsign: str | None = None) -> Observation:
    return c17(h.clock.now(), hex=f"a0000{n}", callsign=callsign or f"BOLT{n}1",
               type_code="ZZZZ", registration=f"N{n}00X", db_flags=0, type_desc=None)  # fmt: skip


async def test_suggestions_api_accept_dismiss_and_backup(authed: Harness) -> None:
    c = authed.client
    await c.post("/api/places", json=HOME)
    rt = authed.ctx.rt()
    await rt.ingest(
        SourceBatch("adsb.fi", authed.clock.now(), [_bolt(authed, n) for n in range(4)])
    )
    for n in range(3):
        r = await c.post(
            "/api/military/overrides", json={"key": f"hex:a0000{n}", "is_military": True}
        )
        assert r.status_code == 201
    row = await authed.ctx.db.fetchone(
        "SELECT snapshot_json FROM military_overrides WHERE key = 'hex:a00000'"
    )
    assert row is not None and json.loads(row[0])["callsign"] == "BOLT01"
    # An old-style override: no snapshot, callsign only in the note, aircraft not tracked.
    await authed.ctx.db.execute(
        "INSERT INTO military_overrides(key, is_military, note, created_at) "
        "VALUES ('hex:a00009', 0, 'HOOK21', 0)"
    )

    data: dict[str, Any] = (await c.get("/api/military/suggestions")).json()
    assert data["marks"] == 4 and data["min_evidence"] == MIN_EVIDENCE
    got = {s["id"]: s for s in data["suggestions"]}
    assert "weight:tactical_callsign:raise" not in got  # HOOK21 (from the note) would flip
    add = got["callsign_prefix:add:BOLT"]
    assert add["live_changes"] == 1  # BOLT31 is tracked, unmarked, and would become military
    assert not rt.store.get("a00003").classification.military  # type: ignore[union-attr]

    r = await c.post("/api/military/suggestions/accept", json={"id": add["id"]})
    assert r.status_code == 200, r.text
    assert "BOLT" in r.json()["callsign_prefixes"]
    assert rt.store.get("a00003").classification.military  # type: ignore[union-attr]
    r = await c.post("/api/military/suggestions/accept", json={"id": add["id"]})
    assert r.status_code == 409

    # Dismissed suggestions stay dismissed until you restore them.
    pats = [_bolt(authed, n, f"PAT{n}01") for n in range(5, 8)]
    await rt.ingest(SourceBatch("adsb.fi", authed.clock.now(), pats))
    for o in pats:  # by registration this time
        await c.post("/api/military/overrides",
                     json={"key": f"reg:{o.registration}", "is_military": False})  # fmt: skip
    data = (await c.get("/api/military/suggestions")).json()
    ids = [s["id"] for s in data["suggestions"]]
    assert ids == ["callsign_prefix:remove:PAT"], data
    r = await c.post("/api/military/suggestions/dismiss", json={"id": ids[0]})
    assert r.json() == {"dismissed": 1}
    data = (await c.get("/api/military/suggestions")).json()
    assert ids[0] not in [s["id"] for s in data["suggestions"]] and data["dismissed"] == 1

    backup = (await c.get("/api/backup")).json()
    assert backup["settings"]["military_suggestions"]["dismissed"] == [ids[0]]
    snaps = {o["key"]: o["snapshot"] for o in backup["military_overrides"]}
    assert snaps["hex:a00001"]["callsign"] == "BOLT11"

    r = await c.put(
        "/api/military/suggestions/settings", json={"min_evidence": 4, "resurface_on_double": True}
    )
    assert r.status_code == 200, r.text
    data = (await c.get("/api/military/suggestions")).json()
    assert data["min_evidence"] == 4 and data["resurface_on_double"]
    assert data["suggestions"] == []  # only 3 PAT marks now
    bad = {"min_evidence": 1, "resurface_on_double": False}
    assert (await c.put("/api/military/suggestions/settings", json=bad)).status_code == 422
    await c.put(
        "/api/military/suggestions/settings", json={"min_evidence": 3, "resurface_on_double": True}
    )

    await c.delete("/api/military/suggestions/dismissed")
    data = (await c.get("/api/military/suggestions")).json()
    assert data["dismissed"] == 0 and ids[0] in [s["id"] for s in data["suggestions"]]
    assert data["resurface_on_double"]  # restoring dismissed keeps the settings

    r = await c.post("/api/backup/restore", json=backup)
    assert r.status_code == 200, r.text
    prefs = await authed.ctx.db.get_json("military_suggestions")
    assert prefs["dismissed"] == [ids[0]] and prefs["dismissed_counts"] == {ids[0]: 3}
