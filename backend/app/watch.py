"""Aircraft followed anywhere: every enabled filter condition "registration is/in …" or
"hex is/in …" adds those aircraft to the watchlist. The poller then looks them up by hex wherever
they are (one small request per minute for the whole list), so such filters work outside your
places. Registrations resolve to hex through the aircraft database; any that don't are looked up
by registration now and then.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.db import Database
from app.filters.models import Condition, FilterOut, Group

WATCH_INTERVAL_S = 60.0
REG_LOOKUP_INTERVAL_S = 300.0
MAX_HEXES_PER_REQUEST = 40
MAX_WATCHED = 200  # a few requests per minute at most, well inside the budget
_FIELDS = {"hex": "hex", "registration": "reg"}


@dataclass
class WatchEntry:
    kind: str  # "hex" or "reg"
    value: str  # hex lowercase, registration uppercase
    filter_ids: set[int] = field(default_factory=set)
    hexes: set[str] = field(default_factory=set)  # resolved addresses
    last_seen: float | None = None
    last_lookup: float = 0.0


def _values(c: Condition) -> list[str]:
    raw = c.value if isinstance(c.value, list) else [c.value]
    return [str(v).strip() for v in raw if v is not None and str(v).strip()]


def targets(filters: list[FilterOut]) -> dict[tuple[str, str], set[int]]:
    """(kind, value) → ids of the filters that ask for it. Only positive, enabled conditions
    count: a condition inside a "none of" group or using "not in" excludes, so it isn't watched."""
    out: dict[tuple[str, str], set[int]] = {}

    def walk(node: Condition | Group, fid: int) -> None:
        if isinstance(node, Group):
            if node.enabled and node.mode != "none":
                for child in node.children:
                    walk(child, fid)
            return
        kind = _FIELDS.get(node.field)
        if not node.enabled or kind is None or node.op not in ("eq", "in"):
            return
        for v in _values(node):
            value = v.lower() if kind == "hex" else v.upper()
            out.setdefault((kind, value), set()).add(fid)

    for f in filters:
        if f.enabled:
            walk(f.definition.root, f.id)
    return out


class Watchlist:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.entries: dict[tuple[str, str], WatchEntry] = {}
        self.next_due = 0.0
        self.last_run: float | None = None
        self.last_found = 0

    async def refresh(self, filters: list[FilterOut]) -> None:
        wanted = dict(list(targets(filters).items())[:MAX_WATCHED])
        old = self.entries
        self.entries = {}
        for key, fids in wanted.items():
            entry = old.get(key) or WatchEntry(*key)
            entry.filter_ids = fids
            self.entries[key] = entry
            if entry.kind == "hex":
                entry.hexes = {entry.value}
        regs = [e.value for e in self.entries.values() if e.kind == "reg"]
        if regs:
            marks = ",".join("?" * len(regs))
            rows = await self.db.fetchall(
                f"SELECT icao, reg FROM aircraft_db WHERE reg IN ({marks})",  # noqa: S608
                tuple(regs),
            )
            found: dict[str, set[str]] = {}
            for r in rows:
                found.setdefault(r["reg"].upper(), set()).add(r["icao"].lower())
            for e in self.entries.values():
                if e.kind == "reg":
                    e.hexes = found.get(e.value, set()) | (e.hexes if e.hexes else set())
        if wanted.keys() - old.keys():
            self.next_due = 0.0  # look new aircraft up right away

    def hexes(self) -> list[str]:
        return sorted({h for e in self.entries.values() for h in e.hexes})

    def due_registration(self, now: float) -> str | None:
        """One unresolved registration whose lookup is due, oldest first."""
        due = [
            e for e in self.entries.values()
            if e.kind == "reg" and not e.hexes and now - e.last_lookup >= REG_LOOKUP_INTERVAL_S
        ]  # fmt: skip
        return min(due, key=lambda x: x.last_lookup).value if due else None

    def looked_up(self, registration: str, now: float) -> None:
        if (e := self.entries.get(("reg", registration))) is not None:
            e.last_lookup = now

    def learn(self, hex_: str, registration: str | None, now: float) -> None:
        """Called for every observation from a watch lookup."""
        for e in self.entries.values():
            if hex_ in e.hexes or (e.kind == "reg" and registration == e.value):
                e.hexes.add(hex_)
                e.last_seen = now

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "kind": e.kind, "value": e.value, "hexes": sorted(e.hexes),
                "filter_ids": sorted(e.filter_ids), "last_seen": e.last_seen,
                "resolved": bool(e.hexes),
            }
            for e in sorted(self.entries.values(), key=lambda x: (x.kind, x.value))
        ]  # fmt: skip
