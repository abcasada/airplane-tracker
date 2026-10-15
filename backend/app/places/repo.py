import json
from typing import Any

import aiosqlite

from app.clock import Clock
from app.db import Database

from .models import Place, PlaceIn

_JSON_FIELDS = ("polygon", "visibility", "schedule")
_COLUMNS = (
    "name", "role", "lat", "lon", "elevation_ft", "radius_nm", "polygon", "alt_floor_ft",
    "alt_ceiling_ft", "airport_icao", "visibility", "active_from", "active_until", "schedule",
    "is_home", "enabled", "source",
)  # fmt: skip


def _to_row(p: PlaceIn) -> list[Any]:
    data = p.model_dump()
    out: list[Any] = []
    for col in _COLUMNS:
        v = data[col]
        if col in _JSON_FIELDS:
            v = json.dumps(v) if v is not None else None
        elif isinstance(v, bool):
            v = int(v)
        out.append(v)
    return out


def _from_row(row: aiosqlite.Row) -> Place:
    data = dict(row)
    for col in _JSON_FIELDS:
        if data.get(col):
            data[col] = json.loads(data[col])
    data["is_home"] = bool(data["is_home"])
    data["enabled"] = bool(data["enabled"])
    return Place.model_validate(data)


class PlaceRepo:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    async def all(self) -> list[Place]:
        rows = await self.db.fetchall("SELECT * FROM places ORDER BY is_home DESC, role, name")
        return [_from_row(r) for r in rows]

    async def get(self, place_id: int) -> Place | None:
        row = await self.db.fetchone("SELECT * FROM places WHERE id = ?", (place_id,))
        return _from_row(row) if row else None

    async def home(self) -> Place | None:
        row = await self.db.fetchone("SELECT * FROM places WHERE is_home = 1 LIMIT 1")
        return _from_row(row) if row else None

    async def create(self, p: PlaceIn) -> Place:
        now = self.clock.now()
        if p.is_home:
            await self.db.execute("UPDATE places SET is_home = 0")
        cols = ", ".join(_COLUMNS)
        marks = ", ".join("?" for _ in _COLUMNS)
        pid = await self.db.insert(
            f"INSERT INTO places({cols}, created_at, updated_at) VALUES ({marks}, ?, ?)",  # noqa: S608
            [*_to_row(p), now, now],
        )
        place = await self.get(pid)
        assert place is not None
        return place

    async def update(self, place_id: int, p: PlaceIn) -> Place | None:
        if await self.get(place_id) is None:
            return None
        if p.is_home:
            await self.db.execute("UPDATE places SET is_home = 0 WHERE id != ?", (place_id,))
        sets = ", ".join(f"{c} = ?" for c in _COLUMNS)
        await self.db.execute(
            f"UPDATE places SET {sets}, updated_at = ? WHERE id = ?",  # noqa: S608
            [*_to_row(p), self.clock.now(), place_id],
        )
        return await self.get(place_id)

    async def delete(self, place_id: int) -> bool:
        return (await self.db.execute("DELETE FROM places WHERE id = ?", (place_id,))) > 0
