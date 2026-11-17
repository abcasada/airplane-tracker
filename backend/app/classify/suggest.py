"""Classifier changes suggested by your military / not-military marks.

Pure: takes each mark with what the classifier saw for that aircraft, re-runs the one classifier
(`classify`) with candidate settings, and proposes list or weight edits. Nothing is applied until
you accept a suggestion, and accepting only edits the same `ClassifierSettings` the review page
edits, so live alerting, the preview and the backtest all follow.
"""

import math
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from pydantic import BaseModel, Field

from .military import (
    DEFAULT_WEIGHTS,
    Classification,
    ClassifierSettings,
    Inputs,
    classify,
    match_callsign_prefix,
    match_keyword,
)

# Default for the "minimum agreeing marks" setting. Two marks can be one formation or one
# operator's pair of airframes; three that agree look like a pattern rather than a coincidence.
MIN_EVIDENCE = 3
MIN_EVIDENCE_RANGE = (2, 20)
# Same step and ceiling as the weight sliders on the review page.
WEIGHT_STEP = 0.05
MAX_WEIGHT = 0.95
# Longer owner texts are usually a full legal name with an address, not a reusable keyword.
MAX_KEYWORD_LEN = 60
MAX_EVIDENCE_LISTED = 12
MAX_DISMISSED = 2000

# Letters then a digit, as the callsign-prefix signal requires (RCH452 → RCH). One letter is
# too broad to suggest (N-numbers, C-registrations).
_PREFIX = re.compile(r"^([A-Z]{2,7})\d")

DUAL_USE = "dual_use_type"
SIGNAL_LABELS: dict[str, str] = {
    "db_flag": "military flag in aircraft database",
    "plane_alert_mil": "plane-alert-db says military",
    "mil_address": "transponder address in a military block",
    "mil_type": "military-only aircraft type",
    "owner_keyword": "military owner/operator words",
    "callsign_prefix": "military callsign prefix",
    "registration_pattern": "registration looks like a military serial",
    "tactical_callsign": "tactical-looking callsign",
    DUAL_USE: "mixed civil/military type (maximum)",
}
WEIGHT_KEYS = [*DEFAULT_WEIGHTS, DUAL_USE]

Kind = Literal[
    "add_callsign_prefix",
    "remove_callsign_prefix",
    "add_owner_keyword",
    "remove_owner_keyword",
    "set_weight",
]


@dataclass(frozen=True, slots=True)
class Mark:
    """One of your overrides, with the classifier inputs for that aircraft."""

    key: str
    is_military: bool
    inputs: Inputs

    @property
    def label(self) -> str:
        i = self.inputs
        if i.callsign and i.registration and i.callsign != i.registration:
            return f"{i.callsign} ({i.registration})"
        return i.callsign or i.registration or self.key


class SuggestionPrefs(BaseModel):
    """Stored under the `military_suggestions` setting; included in backups."""

    dismissed: list[str] = Field(default_factory=list, max_length=MAX_DISMISSED)
    # Evidence count when each suggestion was dismissed (absent for dismissals made before
    # counts were kept; those never come back on their own).
    dismissed_counts: dict[str, int] = Field(default_factory=dict)
    min_evidence: int = Field(MIN_EVIDENCE, ge=MIN_EVIDENCE_RANGE[0], le=MIN_EVIDENCE_RANGE[1])
    # Off by default: a dismissal is final unless you opt in.
    resurface_on_double: bool = False

    def dismiss(self, sug_id: str, count: int | None) -> None:
        if sug_id in self.dismissed:
            self.dismissed.remove(sug_id)
        self.dismissed = [*self.dismissed[-(MAX_DISMISSED - 1) :], sug_id]
        if count is None:
            self.dismissed_counts.pop(sug_id, None)
        else:
            self.dismissed_counts[sug_id] = count
        kept = set(self.dismissed)
        self.dismissed_counts = {k: v for k, v in self.dismissed_counts.items() if k in kept}

    def hides(self, sug: "Suggestion") -> bool:
        if sug.id not in self.dismissed:
            return False
        was = self.dismissed_counts.get(sug.id)
        return not (self.resurface_on_double and was is not None and sug.count >= 2 * was)

    def clear_dismissed(self) -> None:
        self.dismissed = []
        self.dismissed_counts = {}


# Type statistics come from the current dataset at analysis time, not from the snapshot.
_NOT_SNAPSHOTTED = {"override", "type_airframes", "type_military_share"}


def to_snapshot(i: Inputs) -> dict[str, Any]:
    """What the classifier saw for an aircraft when you marked it, as JSON-safe data."""
    return {k: v for k, v in asdict(i).items() if k not in _NOT_SNAPSHOTTED}


def from_snapshot(snap: dict[str, Any], type_stats: tuple[int, float] | None) -> Inputs:
    def text(k: str) -> str | None:
        v = snap.get(k)
        return v if isinstance(v, str) and v else None

    return Inputs(
        hex=text("hex") or "",
        callsign=text("callsign"),
        registration=text("registration"),
        type_code=text("type_code"),
        owner_op=text("owner_op"),
        db_military=bool(snap.get("db_military")),
        in_military_range=bool(snap.get("in_military_range")),
        plane_alert_cmpg=text("plane_alert_cmpg"),
        plane_alert_operator=text("plane_alert_operator"),
        type_airframes=type_stats[0] if type_stats else 0,
        type_military_share=type_stats[1] if type_stats else None,
        callsign_is_airline=bool(snap.get("callsign_is_airline")),
        override=None,
    )


class Suggestion(BaseModel):
    id: str
    kind: Kind
    target: str
    value: float | None = None
    current: float | None = None
    title: str
    reason: str
    count: int
    evidence: list[str]
    # Set when a dismissed suggestion is back because its evidence doubled: the count then.
    dismissed_count: int | None = None


def weight_of(s: ClassifierSettings, key: str) -> float:
    if key == DUAL_USE:
        return s.dual_use_max_weight
    return s.weights.get(key, DEFAULT_WEIGHTS.get(key, 0.0))


def with_weight(s: ClassifierSettings, key: str, value: float) -> ClassifierSettings:
    if key == DUAL_USE:
        return s.model_copy(update={"dual_use_max_weight": value})
    return s.model_copy(update={"weights": {**s.weights, key: value}})


def apply(s: ClassifierSettings, sug: Suggestion) -> ClassifierSettings:
    """The settings after accepting `sug`."""
    t = sug.target
    if sug.kind == "add_callsign_prefix":
        out = s.model_copy(update={"callsign_prefixes": [*s.callsign_prefixes, t]})
    elif sug.kind == "remove_callsign_prefix":
        kept = [p for p in s.callsign_prefixes if p.strip().upper() != t]
        out = s.model_copy(update={"callsign_prefixes": kept})
    elif sug.kind == "add_owner_keyword":
        out = s.model_copy(update={"owner_keywords": [*s.owner_keywords, t]})
    elif sug.kind == "remove_owner_keyword":
        kept = [k for k in s.owner_keywords if k.strip().upper() != t]
        out = s.model_copy(update={"owner_keywords": kept})
    else:
        assert sug.value is not None
        out = with_weight(s, t, sug.value)
    return ClassifierSettings.model_validate(out.model_dump())


def suggest(
    marks: Iterable[Mark], s: ClassifierSettings, prefs: SuggestionPrefs | None = None
) -> list[Suggestion]:
    """Every suggestion your marks support, dismissed ones included; see `visible`."""
    n = (prefs or SuggestionPrefs()).min_evidence
    ms = [replace(m, inputs=replace(m.inputs, override=None)) for m in marks]
    base = {m.key: classify(m.inputs, s) for m in ms}
    out = [
        *_prefix_additions(ms, s, base, n),
        *_prefix_removals(ms, s, n),
        *_owner_additions(ms, s, base, n),
        *_owner_removals(ms, s, n),
        *_weight_changes(ms, s, base, n),
    ]
    out.sort(key=lambda x: (-x.count, x.id))
    return out


def visible(items: list[Suggestion], prefs: SuggestionPrefs) -> list[Suggestion]:
    """Drops dismissed suggestions, except those back because their evidence doubled."""
    out = []
    for x in items:
        if prefs.hides(x):
            continue
        if x.id in prefs.dismissed:
            x = x.model_copy(update={"dismissed_count": prefs.dismissed_counts[x.id]})
        out.append(x)
    return out


def _evidence(ms: list[Mark]) -> list[str]:
    return sorted({m.label for m in ms})[:MAX_EVIDENCE_LISTED]


def _below(ms: list[Mark], base: dict[str, Classification]) -> str:
    n = sum(1 for m in ms if not base[m.key].military)
    return f" Without your marks, {n} of them score below the threshold." if n else ""


def _owner(m: Mark) -> str | None:
    return " ".join(m.inputs.owner_op.upper().split()) if m.inputs.owner_op else None


def _prefix_additions(
    ms: list[Mark], s: ClassifierSettings, base: dict[str, Classification], min_n: int
) -> list[Suggestion]:
    groups: dict[str, list[Mark]] = defaultdict(list)
    for m in ms:
        cs = m.inputs.callsign
        if cs and cs != (m.inputs.registration or "") and (hit := _PREFIX.match(cs)):
            groups[hit.group(1)].append(m)
    out = []
    for prefix, group in groups.items():
        if any(not m.is_military for m in group) or len(group) < min_n:
            continue
        if match_callsign_prefix(f"{prefix}1", s.callsign_prefixes):
            continue  # already on the list
        out.append(
            Suggestion(
                id=f"callsign_prefix:add:{prefix}",
                kind="add_callsign_prefix",
                target=prefix,
                title=f"Add {prefix} to the military callsign prefixes",
                reason=f"You marked {len(group)} aircraft with {prefix}+number callsigns military "
                f"and none not military.{_below(group, base)}",
                count=len(group),
                evidence=_evidence(group),
            )
        )
    return out


def _prefix_removals(ms: list[Mark], s: ClassifierSettings, min_n: int) -> list[Suggestion]:
    groups: dict[str, list[Mark]] = defaultdict(list)
    for m in ms:
        if p := match_callsign_prefix(m.inputs.callsign, s.callsign_prefixes):
            groups[p].append(m)
    out = []
    for prefix, group in groups.items():
        if any(m.is_military for m in group) or len(group) < min_n:
            continue
        out.append(
            Suggestion(
                id=f"callsign_prefix:remove:{prefix}",
                kind="remove_callsign_prefix",
                target=prefix,
                title=f"Remove {prefix} from the military callsign prefixes",
                reason=f"You marked {len(group)} aircraft with {prefix}+number callsigns not "
                "military and none military.",
                count=len(group),
                evidence=_evidence(group),
            )
        )
    return out


def _owner_additions(
    ms: list[Mark], s: ClassifierSettings, base: dict[str, Classification], min_n: int
) -> list[Suggestion]:
    groups: dict[str, list[Mark]] = defaultdict(list)
    for m in ms:
        owner = _owner(m)
        if not (m.is_military and owner and len(owner) <= MAX_KEYWORD_LEN):
            continue
        if not match_keyword(owner, s.owner_keywords):
            groups[owner].append(m)
    out = []
    for owner, group in groups.items():
        if len(group) < min_n:
            continue
        if any(not m.is_military and match_keyword(_owner(m), [owner]) for m in ms):
            continue
        out.append(
            Suggestion(
                id=f"owner_keyword:add:{owner}",
                kind="add_owner_keyword",
                target=owner,
                title=f'Add "{owner}" to the military owner words',
                reason=f'You marked {len(group)} aircraft owned or operated by "{owner}" military, '
                f"and no owner word matches it yet.{_below(group, base)}",
                count=len(group),
                evidence=_evidence(group),
            )
        )
    return out


def _owner_removals(ms: list[Mark], s: ClassifierSettings, min_n: int) -> list[Suggestion]:
    out = []
    for raw in s.owner_keywords:
        kw = raw.strip().upper()
        if not kw:
            continue
        group = [m for m in ms if match_keyword(_owner(m), [kw])]
        if any(m.is_military for m in group) or len(group) < min_n:
            continue
        out.append(
            Suggestion(
                id=f"owner_keyword:remove:{kw}",
                kind="remove_owner_keyword",
                target=kw,
                title=f'Remove "{kw}" from the military owner words',
                reason=f'You marked {len(group)} aircraft whose owner/operator mentions "{kw}" '
                "not military and none military.",
                count=len(group),
                evidence=_evidence(group),
            )
        )
    return out


def _grid(current: float, down: bool) -> list[float]:
    """Slider positions from the one next to `current` outwards."""
    if down:
        start = math.ceil(current / WEIGHT_STEP - 1e-9) - 1
        return [round(k * WEIGHT_STEP, 2) for k in range(start, -1, -1)]
    start = math.floor(current / WEIGHT_STEP + 1e-9) + 1
    stop = round(MAX_WEIGHT / WEIGHT_STEP)
    return [round(k * WEIGHT_STEP, 2) for k in range(start, stop + 1)]


def _best_weight(
    fired: list[Mark],
    s: ClassifierSettings,
    key: str,
    base: dict[str, Classification],
    candidates: list[float],
    min_n: int,
) -> tuple[float, list[Mark]] | None:
    """The smallest change that brings the most marks in line without breaking any other mark.

    The score only moves one way as a weight moves, so the first candidate that flips a mark the
    classifier already got right ends the search.
    """
    best: tuple[float, list[Mark]] | None = None
    for v in candidates:
        s2 = with_weight(s, key, v)
        fixed: list[Mark] = []
        broken = False
        for m in fired:
            was_right = base[m.key].military == m.is_military
            now_right = classify(m.inputs, s2).military == m.is_military
            if was_right and not now_right:
                broken = True
                break
            if now_right and not was_right:
                fixed.append(m)
        if broken:
            break
        if len(fixed) > (len(best[1]) if best else 0):
            best = (v, fixed)
    return best if best and len(best[1]) >= min_n else None


def _weight_changes(
    ms: list[Mark], s: ClassifierSettings, base: dict[str, Classification], min_n: int
) -> list[Suggestion]:
    out = []
    for key in WEIGHT_KEYS:
        fired = [m for m in ms if key in base[m.key].signals]
        wrong_yes = [m for m in fired if not m.is_military and base[m.key].military]
        wrong_no = [m for m in fired if m.is_military and not base[m.key].military]
        cur = weight_of(s, key)
        lower = higher = None
        if len(wrong_yes) >= min_n:
            lower = _best_weight(fired, s, key, base, _grid(cur, down=True), min_n)
        if len(wrong_no) >= min_n:
            higher = _best_weight(fired, s, key, base, _grid(cur, down=False), min_n)
        if lower and higher:
            continue  # your marks pull this clue both ways; nothing to suggest
        label = SIGNAL_LABELS.get(key, key)
        if lower:
            v, fixed = lower
            out.append(
                Suggestion(
                    id=f"weight:{key}:lower",
                    kind="set_weight",
                    target=key,
                    value=v,
                    current=cur,
                    title=f'Lower "{label}" from {cur:.2f} to {v:.2f}',
                    reason=f"{len(wrong_yes)} aircraft you marked not military score as military "
                    f"with this clue. At {v:.2f}, {len(fixed)} of them drop below the threshold "
                    "and none of your other marks change.",
                    count=len(fixed),
                    evidence=_evidence(fixed),
                )
            )
        if higher:
            v, fixed = higher
            out.append(
                Suggestion(
                    id=f"weight:{key}:raise",
                    kind="set_weight",
                    target=key,
                    value=v,
                    current=cur,
                    title=f'Raise "{label}" from {cur:.2f} to {v:.2f}',
                    reason=f"{len(wrong_no)} aircraft you marked military have this clue but score "
                    f"below the threshold. At {v:.2f}, {len(fixed)} of them reach it and none of "
                    "your other marks change.",
                    count=len(fixed),
                    evidence=_evidence(fixed),
                )
            )
    return out
