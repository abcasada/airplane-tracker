"""Facts: lazily computed, memoized field values for one aircraft at one moment.

`LiveFacts` computes from a Track; `SnapshotFacts` replays values stored with an alert, so
"why did this fire?" re-evaluates exactly what the engine saw.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

from app.places.models import Place

if TYPE_CHECKING:
    from app.tracks.store import Track

    from .fields import FieldDef


@dataclass
class EvalContext:
    now: float
    places: dict[int, Place]
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))
    extras: dict[str, Any] = field(default_factory=dict)


# Pseudo place id: "any active viewpoint" (home or a travel viewpoint).
ANY_VIEWPOINT = 0


def active_viewpoints(ctx: EvalContext) -> list[Place]:
    return [p for p in ctx.places.values() if p.role == "viewpoint" and p.is_active(ctx.now)]


def display_place(place_id: int | None, facts: LiveFacts) -> Place | None:
    """The place notification text measures from; "any viewpoint" means the nearest one."""
    if place_id is None:
        return None
    if place_id != ANY_VIEWPOINT:
        return facts.ctx.places.get(place_id)
    candidates = [
        (d, p)
        for p in active_viewpoints(facts.ctx)
        if (d := facts.get("distance_nm", p.id)) is not None
    ]
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def fact_key(field_key: str, place_id: int | None) -> str:
    return f"{field_key}@{place_id}" if place_id is not None else field_key


class Facts(Protocol):
    hex: str

    def get(self, field_key: str, place_id: int | None = None) -> Any: ...


class LiveFacts:
    def __init__(self, track: Track, ctx: EvalContext) -> None:
        self.track = track
        self.ctx = ctx
        self.hex = track.hex
        self._memo: dict[str, Any] = {}

    def get(self, field_key: str, place_id: int | None = None) -> Any:
        from .fields import FIELDS

        k = fact_key(field_key, place_id)
        if k in self._memo:
            return self._memo[k]
        fd: FieldDef | None = FIELDS.get(field_key)
        value: Any = None
        if fd is not None:
            if fd.place and place_id == ANY_VIEWPOINT:
                value = self._any_viewpoint(fd)
            elif fd.place:
                place = self.ctx.places.get(place_id) if place_id is not None else None
                if place is not None and (not fd.place_roles or place.role in fd.place_roles):
                    value = fd.compute(self, place)
            else:
                value = fd.compute(self, None)
        self._memo[k] = value
        return value

    def _any_viewpoint(self, fd: FieldDef) -> Any:
        if fd.aggregate is None or (fd.place_roles and "viewpoint" not in fd.place_roles):
            return None
        values = [
            v for p in active_viewpoints(self.ctx) if (v := self.get(fd.key, p.id)) is not None
        ]
        if not values:
            return None
        if fd.aggregate == "any":
            return any(values)
        if fd.aggregate == "min":
            return min(values)
        return max(values)

    def snapshot(self, keys: set[tuple[str, int | None]]) -> dict[str, Any]:
        return {fact_key(f, p): _jsonable(self.get(f, p)) for f, p in keys}


class SnapshotFacts:
    def __init__(self, hex_: str, values: dict[str, Any]) -> None:
        self.hex = hex_
        self.values = values

    def get(self, field_key: str, place_id: int | None = None) -> Any:
        return self.values.get(fact_key(field_key, place_id))


def _jsonable(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, frozenset | set | tuple):
        return sorted(v) if isinstance(v, frozenset | set) else list(v)
    return v
