import json
from typing import Any

import aiosqlite

from app.clock import Clock
from app.db import Database

from .models import FilterDefinition, FilterIn, FilterOut


def _out(row: aiosqlite.Row) -> FilterOut:
    return FilterOut(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        enabled=bool(row["enabled"]),
        mode=row["mode"],
        sort_order=row["sort_order"],
        version=row["version"],
        version_id=row["version_id"],
        definition=FilterDefinition.model_validate_json(row["definition"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_SELECT = (
    "SELECT f.*, v.id AS version_id, v.version, v.definition FROM filters f "
    "JOIN filter_versions v ON v.id = f.current_version_id"
)


class FilterRepo:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    async def all(self) -> list[FilterOut]:
        rows = await self.db.fetchall(f"{_SELECT} ORDER BY f.sort_order, f.id")
        return [_out(r) for r in rows]

    async def get(self, filter_id: int) -> FilterOut | None:
        row = await self.db.fetchone(f"{_SELECT} WHERE f.id = ?", (filter_id,))
        return _out(row) if row else None

    async def _new_version(self, filter_id: int, body: FilterIn) -> int:
        row = await self.db.fetchone(
            "SELECT coalesce(max(version), 0) + 1 FROM filter_versions WHERE filter_id = ?",
            (filter_id,),
        )
        version = row[0] if row else 1
        return await self.db.insert(
            "INSERT INTO filter_versions(filter_id, version, definition, name, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                filter_id,
                version,
                body.definition.model_dump_json(),
                body.name,
                body.note,
                self.clock.now(),
            ),
        )

    async def create(self, body: FilterIn) -> FilterOut:
        now = self.clock.now()
        row = await self.db.fetchone("SELECT coalesce(max(sort_order), 0) + 1 FROM filters")
        fid = await self.db.insert(
            "INSERT INTO filters(name, description, enabled, mode, sort_order, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                body.name,
                body.description,
                int(body.enabled),
                body.mode,
                row[0] if row else 1,
                now,
                now,
            ),
        )
        vid = await self._new_version(fid, body)
        await self.db.execute("UPDATE filters SET current_version_id = ? WHERE id = ?", (vid, fid))
        out = await self.get(fid)
        assert out is not None
        return out

    async def update(self, filter_id: int, body: FilterIn) -> FilterOut | None:
        current = await self.get(filter_id)
        if current is None:
            return None
        vid = current.version_id
        if current.definition != body.definition or current.name != body.name:
            vid = await self._new_version(filter_id, body)
        await self.db.execute(
            "UPDATE filters SET name = ?, description = ?, enabled = ?, mode = ?, "
            "current_version_id = ?, updated_at = ? WHERE id = ?",
            (
                body.name,
                body.description,
                int(body.enabled),
                body.mode,
                vid,
                self.clock.now(),
                filter_id,
            ),
        )
        return await self.get(filter_id)

    async def set_flags(
        self, filter_id: int, enabled: bool | None = None, mode: str | None = None
    ) -> FilterOut | None:
        if enabled is not None:
            await self.db.execute(
                "UPDATE filters SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), self.clock.now(), filter_id),
            )
        if mode is not None:
            await self.db.execute(
                "UPDATE filters SET mode = ?, updated_at = ? WHERE id = ?",
                (mode, self.clock.now(), filter_id),
            )
        return await self.get(filter_id)

    async def delete(self, filter_id: int) -> bool:
        return await self.db.execute("DELETE FROM filters WHERE id = ?", (filter_id,)) > 0

    async def versions(self, filter_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT id, version, name, note, created_at, definition FROM filter_versions "
            "WHERE filter_id = ? ORDER BY version DESC",
            (filter_id,),
        )
        return [{**dict(r), "definition": json.loads(r["definition"])} for r in rows]

    async def revert(self, filter_id: int, version_id: int) -> FilterOut | None:
        row = await self.db.fetchone(
            "SELECT * FROM filter_versions WHERE id = ? AND filter_id = ?", (version_id, filter_id)
        )
        current = await self.get(filter_id)
        if row is None or current is None:
            return None
        body = FilterIn(
            name=row["name"],
            description=current.description,
            enabled=current.enabled,
            mode=current.mode,
            definition=FilterDefinition.model_validate_json(row["definition"]),
            note=f"Reverted to version {row['version']}",
        )
        vid = await self._new_version(filter_id, body)
        await self.db.execute(
            "UPDATE filters SET name = ?, current_version_id = ?, updated_at = ? WHERE id = ?",
            (body.name, vid, self.clock.now(), filter_id),
        )
        return await self.get(filter_id)

    async def reorder(self, ids: list[int]) -> None:
        for i, fid in enumerate(ids):
            await self.db.execute("UPDATE filters SET sort_order = ? WHERE id = ?", (i, fid))
