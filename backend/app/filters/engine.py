"""Evaluates filter expression trees. One engine serves live alerting, the dashboard preview,
funnel and "why not?", alert explanations, and backtests, so they always agree.

Missing values: positive tests (lt, in, glob, is_true, …) fail; negative tests (ne, not_in,
not_glob, contains_none, not_exists) pass, because an unknown operator is "not X".
A filter with no enabled conditions matches nothing, so a blank filter can't spam you.
"""

from __future__ import annotations

import fnmatch
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import regex

from app.places.models import Place

from .facts import Facts
from .fields import FIELDS, OPS_BY_TYPE, FieldDef
from .models import Condition, FilterDefinition, Group

MAX_REGEX_LEN = 200
REGEX_TIMEOUT_S = 0.01

OP_TEXT = {
    "lt": "<", "le": "≤", "gt": ">", "ge": "≥", "eq": "=", "ne": "≠", "between": "between",
    "exists": "is known", "not_exists": "is unknown", "in": "is", "not_in": "is not",
    "glob": "matches", "not_glob": "doesn't match", "contains": "contains",
    "regex": "matches pattern", "contains_any": "includes any of",
    "contains_all": "includes all of", "contains_none": "includes none of",
}  # fmt: skip
NEGATIVE_OPS = {"ne", "not_in", "not_glob", "contains_none", "not_exists"}


class FilterError(ValueError):
    pass


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list | tuple) else [value]


def _upper_list(value: Any) -> list[str]:
    return [str(v).strip().upper() for v in _as_list(value) if str(v).strip()]


def _num(value: Any, what: str) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError) as e:
        raise FilterError(f"{what} needs a number") from e
    if not math.isfinite(n):
        raise FilterError(f"{what} needs a finite number")
    return n


def _make_test(fd: FieldDef, op: str, value: Any) -> Callable[[Any], bool]:
    label = fd.label
    if op not in OPS_BY_TYPE[fd.type]:
        raise FilterError(f"'{label}' can't use '{op}'")
    if op == "exists":
        return lambda v: v is not None and v != [] and v != ""
    if op == "not_exists":
        return lambda v: v is None or v == [] or v == ""
    if op == "is_true":
        return lambda v: v is True
    if op == "is_false":
        return lambda v: v is False
    if op in ("lt", "le", "gt", "ge", "eq", "ne") and fd.type == "number":
        n = _num(value, label)
        cmp: dict[str, Callable[[float], bool]] = {
            "lt": lambda v: v < n, "le": lambda v: v <= n, "gt": lambda v: v > n,
            "ge": lambda v: v >= n, "eq": lambda v: v == n, "ne": lambda v: v != n,
        }  # fmt: skip
        fn = cmp[op]
        if op == "ne":
            return lambda v: v is None or fn(v)
        return lambda v: v is not None and fn(v)
    if op == "between":
        vals = _as_list(value)
        if len(vals) != 2:
            raise FilterError(f"'{label}' between needs two numbers")
        lo, hi = _num(vals[0], label), _num(vals[1], label)
        if lo <= hi:
            return lambda v: v is not None and lo <= v <= hi
        # Wrap-around range, e.g. 22 → 6 o'clock or 350° → 10°.
        return lambda v: v is not None and (v >= lo or v <= hi)
    if op in ("in", "not_in"):
        wanted = set(_upper_list(value))
        if not wanted:
            raise FilterError(f"'{label}' needs at least one value")
        if op == "in":
            return lambda v: v is not None and str(v).upper() in wanted
        return lambda v: v is None or str(v).upper() not in wanted
    if op in ("glob", "not_glob"):
        pats = _upper_list(value)
        if not pats:
            raise FilterError(f"'{label}' needs at least one pattern")

        def globbed(v: Any) -> bool:
            s = str(v).upper()
            return any(fnmatch.fnmatchcase(s, p) for p in pats)

        if op == "glob":
            return lambda v: v is not None and globbed(v)
        return lambda v: v is None or not globbed(v)
    if op == "contains":
        parts = _upper_list(value)
        if not parts:
            raise FilterError(f"'{label}' needs some text")
        return lambda v: v is not None and any(p in str(v).upper() for p in parts)
    if op == "regex":
        pattern = str(value or "")
        if not pattern or len(pattern) > MAX_REGEX_LEN:
            raise FilterError(f"'{label}' needs a pattern of 1–{MAX_REGEX_LEN} characters")
        try:
            rx = regex.compile(pattern, regex.IGNORECASE)
        except regex.error as e:
            raise FilterError(f"'{label}': invalid pattern ({e})") from e

        def rx_test(v: Any) -> bool:
            if v is None:
                return False
            try:
                return rx.search(str(v), timeout=REGEX_TIMEOUT_S) is not None
            except TimeoutError:
                return False

        return rx_test
    if op in ("contains_any", "contains_all", "contains_none"):
        wanted = set(_upper_list(value))
        if not wanted:
            raise FilterError(f"'{label}' needs at least one value")

        def have(v: Any) -> set[str]:
            return {str(x).upper() for x in _as_list(v)}

        if op == "contains_any":
            return lambda v: bool(have(v) & wanted)
        if op == "contains_all":
            return lambda v: wanted <= have(v)
        return lambda v: not (have(v) & wanted)
    raise FilterError(f"'{label}' can't use '{op}'")


@dataclass
class PreparedCond:
    cond: Condition
    fd: FieldDef
    test: Callable[[Any], bool]


@dataclass
class PreparedGroup:
    group: Group
    children: list[PreparedCond | PreparedGroup]


Prepared = PreparedCond | PreparedGroup


def _prepare(node: Condition | Group, places: dict[int, Place], errors: list[str]) -> Prepared:
    if isinstance(node, Group):
        return PreparedGroup(node, [_prepare(c, places, errors) for c in node.children])
    fd = FIELDS.get(node.field)
    if fd is None:
        errors.append(f"Unknown field '{node.field}'")
        return PreparedCond(node, _unknown_field(node.field), lambda _v: False)
    if fd.place:
        place = places.get(node.place) if node.place is not None else None
        if place is None:
            errors.append(f"'{fd.label}' needs a place")
        elif fd.place_roles and place.role not in fd.place_roles:
            errors.append(f"'{fd.label}' only works with a {' or '.join(fd.place_roles)}")
    try:
        test = _make_test(fd, node.op, node.value)
    except FilterError as e:
        errors.append(str(e))
        test = _never
    return PreparedCond(node, fd, test)


def _never(_v: Any) -> bool:
    return False


def _unknown_field(key: str) -> FieldDef:
    return FieldDef(key, key, "Unknown", "string", "", lambda _f, _p: None)


class CompiledFilter:
    def __init__(self, definition: FilterDefinition, places: dict[int, Place]) -> None:
        self.definition = definition
        self.places = places
        self.errors: list[str] = []
        root = _prepare(definition.root, places, self.errors)
        assert isinstance(root, PreparedGroup)
        self.root = root

    @property
    def valid(self) -> bool:
        return not self.errors

    # ---- evaluation ------------------------------------------------------------------------

    def _eval(self, node: Prepared, facts: Facts) -> bool | None:
        """True/False, or None for an empty or disabled node (neutral)."""
        if isinstance(node, PreparedCond):
            if not node.cond.enabled:
                return None
            return node.test(facts.get(node.cond.field, node.cond.place if node.fd.place else None))
        if not node.group.enabled:
            return None
        results = [r for c in node.children if (r := self._eval(c, facts)) is not None]
        if not results:
            return None
        if node.group.mode == "all":
            return all(results)
        if node.group.mode == "any":
            return any(results)
        return not any(results)

    def matches(self, facts: Facts) -> bool:
        if self.errors:
            return False
        return self._eval(self.root, facts) is True

    def explain(self, facts: Facts) -> dict[str, Any]:
        return self._explain(self.root, facts)

    def _explain(self, node: Prepared, facts: Facts) -> dict[str, Any]:
        if isinstance(node, PreparedCond):
            c = node.cond
            place = c.place if node.fd.place else None
            value = facts.get(c.field, place)
            return {
                "id": c.id,
                "type": "cond",
                "text": describe_condition(c, self.places),
                "value": value if not isinstance(value, frozenset) else sorted(value),
                "result": node.test(value) if c.enabled else None,
                "enabled": c.enabled,
            }
        return {
            "id": node.group.id,
            "type": "group",
            "mode": node.group.mode,
            "enabled": node.group.enabled,
            "result": self._eval(node, facts),
            "children": [self._explain(ch, facts) for ch in node.children],
        }

    def funnel(self, facts_list: list[Facts]) -> list[dict[str, Any]]:
        """For an "all" root: how many aircraft remain after each condition, in order.
        For "any"/"none" roots: how many each top-level condition matches on its own."""
        steps: list[dict[str, Any]] = [
            {"id": None, "text": "Aircraft in range", "count": len(facts_list)}
        ]
        remaining = list(facts_list)
        for child in self.root.children:
            text = (
                describe_condition(child.cond, self.places)
                if isinstance(child, PreparedCond)
                else describe_group(child.group, self.places)
            )
            if self.root.group.mode == "all":
                kept = [f for f in remaining if self._eval(child, f) is not False]
                remaining = kept
                steps.append({"id": _node_id(child), "text": text, "count": len(kept)})
            else:
                n = sum(1 for f in facts_list if self._eval(child, f) is True)
                steps.append({"id": _node_id(child), "text": text, "count": n})
        return steps

    def referenced(self) -> set[tuple[str, int | None]]:
        out: set[tuple[str, int | None]] = set()

        def walk(node: Prepared) -> None:
            if isinstance(node, PreparedCond):
                out.add((node.cond.field, node.cond.place if node.fd.place else None))
            else:
                for c in node.children:
                    walk(c)

        walk(self.root)
        return out


def _node_id(node: Prepared) -> str:
    return node.cond.id if isinstance(node, PreparedCond) else node.group.id


# ---- plain-English descriptions ----------------------------------------------------------------


def _fmt_value(v: Any) -> str:
    if isinstance(v, list | tuple):
        return ", ".join(str(x) for x in v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def describe_condition(c: Condition, places: dict[int, Place]) -> str:
    fd = FIELDS.get(c.field)
    if fd is None:
        return f"unknown field {c.field}"
    label = fd.label
    if fd.place:
        p = places.get(c.place) if c.place is not None else None
        label = f"{label} ({'from ' if fd.key != 'inside_area' else ''}{p.name if p else '?'})"
    unit = f" {fd.unit}" if fd.unit and fd.unit not in ("°", "h") else (fd.unit or "")
    if c.op == "is_true":
        text = label
    elif c.op == "is_false":
        text = f"not {label}"
    elif c.op in ("exists", "not_exists"):
        text = f"{label} {OP_TEXT[c.op]}"
    elif c.op == "between":
        vals = _as_list(c.value)
        lo, hi = [*vals, None, None][:2]
        text = f"{label} between {_fmt_value(lo)} and {_fmt_value(hi)}{unit}"
    elif fd.type == "number":
        text = f"{label} {OP_TEXT[c.op]} {_fmt_value(c.value)}{unit}"
    else:
        text = f"{label} {OP_TEXT[c.op]} {_fmt_value(c.value)}"
    return text if c.enabled else f"({text} — off)"


def describe_group(g: Group, places: dict[int, Place], depth: int = 0) -> str:
    parts = [
        describe_condition(ch, places)
        if isinstance(ch, Condition)
        else describe_group(ch, places, depth + 1)
        for ch in g.children
        if ch.enabled
    ]
    if not parts:
        return "(nothing)"
    joiner = {"all": " AND ", "any": " OR ", "none": " OR "}[g.mode]
    body = joiner.join(parts)
    if g.mode == "none":
        body = f"NOT ({body})"
    elif depth > 0 and len(parts) > 1:
        body = f"({body})"
    return body


def describe(definition: FilterDefinition, places: dict[int, Place]) -> str:
    return describe_group(definition.root, places)


def compile_many(
    defs: Iterable[tuple[int, FilterDefinition]], places: dict[int, Place]
) -> dict[int, CompiledFilter]:
    return {fid: CompiledFilter(d, places) for fid, d in defs}
