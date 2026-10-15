"""Explainable military / government / police classification.

Independent signals each carry a weight; the score is 1 − ∏(1 − w) over the signals that fire.
Every signal is also exposed as a filter field, so users decide how strict to be.
"""

import re
from dataclasses import dataclass
from functools import lru_cache

import regex
from pydantic import BaseModel, Field

# Weights follow docs/PLAN.md §5.2.
DEFAULT_WEIGHTS: dict[str, float] = {
    "db_flag": 0.9,
    "plane_alert_mil": 0.9,
    "mil_address": 0.85,
    "mil_type": 0.8,
    "owner_keyword": 0.8,
    "callsign_prefix": 0.6,
    "registration_pattern": 0.6,
    "tactical_callsign": 0.3,
}

# Well-known military callsign prefixes; a digit must follow (RCH123, SAM491).
DEFAULT_CALLSIGN_PREFIXES = [
    "RCH", "REACH", "SAM", "SPAR", "EXEC", "VENUS", "PAT", "CNV", "NAVY", "ARMY", "AF",
    "CFC", "RRR", "ASCOT", "GAF", "BAF", "CTM", "IAM", "NATO", "MMF", "HAF", "PLF",
]  # fmt: skip
DEFAULT_REGISTRATION_PATTERNS = [
    r"^\d{2}-\d{4,5}$",  # US military serial, e.g. 98-0001
    r"^Z[A-Z]\d{3}$",  # UK military, e.g. ZZ336
]
DEFAULT_OWNER_KEYWORDS = [
    "AIR FORCE", "NAVY", "ARMY", "MARINE CORPS", "NATIONAL GUARD", "COAST GUARD",
    "DEPARTMENT OF DEFENSE", "MINISTRY OF DEFENCE", "LUFTWAFFE", "ROYAL AIR FORCE",
]  # fmt: skip
DEFAULT_GOVERNMENT_KEYWORDS = [
    "FEDERAL AVIATION ADMINISTRATION", "CUSTOMS AND BORDER", "DEPARTMENT OF", "DEPT OF",
    "STATE OF ", "UNITED STATES GOVERNMENT", "U S GOVERNMENT", "NATIONAL AERONAUTICS",
]  # fmt: skip
DEFAULT_POLICE_KEYWORDS = [
    "POLICE", "SHERIFF", "HIGHWAY PATROL", "STATE PATROL", "CONSTABULARY",
]  # fmt: skip

MIL_TYPE_SHARE = 0.95
DUAL_USE_MIN_SHARE = 0.15
MIN_TYPE_AIRFRAMES = 15
_TACTICAL = re.compile(r"^([A-Z]{3,6})(\d{1,3})$")


class ClassifierSettings(BaseModel):
    threshold: float = Field(0.6, ge=0, le=1)
    weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    dual_use_max_weight: float = Field(0.5, ge=0, le=1)
    callsign_prefixes: list[str] = Field(default_factory=lambda: list(DEFAULT_CALLSIGN_PREFIXES))
    registration_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_REGISTRATION_PATTERNS)
    )
    owner_keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_OWNER_KEYWORDS))
    government_keywords: list[str] = Field(
        default_factory=lambda: list(DEFAULT_GOVERNMENT_KEYWORDS)
    )
    police_keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_POLICE_KEYWORDS))


@dataclass(frozen=True, slots=True)
class Inputs:
    hex: str
    callsign: str | None
    registration: str | None
    type_code: str | None
    owner_op: str | None
    db_military: bool
    in_military_range: bool
    plane_alert_cmpg: str | None
    plane_alert_operator: str | None
    type_airframes: int
    type_military_share: float | None
    callsign_is_airline: bool
    override: bool | None


@dataclass(frozen=True, slots=True)
class Classification:
    military_score: float
    military: bool
    government: bool
    police: bool
    signals: frozenset[str]
    reasons: tuple[str, ...]
    override: bool | None = None


@lru_cache(maxsize=256)
def _keyword_regex(words: tuple[str, ...]) -> re.Pattern[str] | None:
    words = tuple(w.strip().upper() for w in words if w.strip())
    if not words:
        return None
    return re.compile("|".join(rf"(?<![A-Z]){re.escape(w)}(?![A-Z])" for w in words))


@lru_cache(maxsize=64)
def _compiled_patterns(patterns: tuple[str, ...]) -> tuple[regex.Pattern[str], ...]:
    out = []
    for p in patterns:
        try:
            out.append(regex.compile(p[:200]))
        except regex.error:
            continue
    return tuple(out)


def _matches_keywords(text: str | None, words: list[str]) -> str | None:
    if not text:
        return None
    rx = _keyword_regex(tuple(words))
    m = rx.search(text.upper()) if rx else None
    return m.group(0) if m else None


def _callsign_prefix(callsign: str | None, prefixes: list[str]) -> str | None:
    if not callsign:
        return None
    for p in sorted(prefixes, key=len, reverse=True):
        p = p.strip().upper()
        if p and callsign.startswith(p):
            rest = callsign[len(p) :]
            if rest == "" or rest[0].isdigit():
                return p
    return None


def classify(i: Inputs, s: ClassifierSettings) -> Classification:
    w = s.weights
    fired: dict[str, float] = {}
    reasons: list[str] = []

    def fire(key: str, weight: float, reason: str) -> None:
        if weight > 0:
            fired[key] = weight
            reasons.append(reason)

    if i.db_military:
        fire("db_flag", w.get("db_flag", 0.9), "military flag in aircraft database")
    if (i.plane_alert_cmpg or "").lower() == "mil":
        who = f" ({i.plane_alert_operator})" if i.plane_alert_operator else ""
        fire("plane_alert_mil", w.get("plane_alert_mil", 0.9), f"plane-alert-db: military{who}")
    if i.in_military_range:
        fire("mil_address", w.get("mil_address", 0.85), "transponder address in a military block")
    share = i.type_military_share
    if share is not None and i.type_airframes >= MIN_TYPE_AIRFRAMES and i.type_code:
        if share >= MIL_TYPE_SHARE:
            fire("mil_type", w.get("mil_type", 0.8), f"type {i.type_code} is military-only")
        elif share >= DUAL_USE_MIN_SHARE:
            fire(
                "dual_use_type",
                s.dual_use_max_weight * share,
                f"type {i.type_code}: {round(share * 100)}% of airframes are military",
            )
    if kw := _matches_keywords(i.owner_op, s.owner_keywords):
        fire("owner_keyword", w.get("owner_keyword", 0.8), f'owner/operator mentions "{kw}"')
    if p := _callsign_prefix(i.callsign, s.callsign_prefixes):
        fire("callsign_prefix", w.get("callsign_prefix", 0.6), f"military callsign prefix {p}")
    elif (
        i.callsign
        and not i.callsign_is_airline
        and i.callsign != (i.registration or "")
        and _TACTICAL.match(i.callsign)
    ):
        fire(
            "tactical_callsign",
            w.get("tactical_callsign", 0.3),
            f"callsign {i.callsign} looks tactical (word + number, not an airline)",
        )
    if i.registration:
        reg = i.registration.upper()
        for rx in _compiled_patterns(tuple(s.registration_patterns)):
            if rx.match(reg, timeout=0.01):
                fire(
                    "registration_pattern",
                    w.get("registration_pattern", 0.6),
                    f"registration {reg} looks like a military serial",
                )
                break

    remaining = 1.0
    for weight in fired.values():
        remaining *= 1.0 - min(max(weight, 0.0), 1.0)
    score = round(1.0 - remaining, 3)

    government = (i.plane_alert_cmpg or "").lower() == "gov" or bool(
        _matches_keywords(i.owner_op, s.government_keywords)
    )
    police = (i.plane_alert_cmpg or "").lower() == "pol" or bool(
        _matches_keywords(i.owner_op, s.police_keywords)
    )
    military = score >= s.threshold
    if i.override is not None:
        military = i.override
        reasons.insert(0, "marked military by you" if i.override else "marked not military by you")
    return Classification(
        military_score=score,
        military=military,
        government=government and not military,
        police=police,
        signals=frozenset(fired),
        reasons=tuple(reasons),
        override=i.override,
    )
