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
            if fd.place:
                place = self.ctx.places.get(place_id) if place_id is not None else None
                if place is not None and (not fd.place_roles or place.role in fd.place_roles):
                    value = fd.compute(self, place)
            else:
                value = fd.compute(self, None)
        self._memo[k] = value
        return value

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
