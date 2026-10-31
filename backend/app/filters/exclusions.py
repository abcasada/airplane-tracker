"""Quick mutes: "never notify me about this aircraft / type / operator", globally or per filter."""

import fnmatch
from typing import Literal

from pydantic import BaseModel, Field

from app.clock import Clock
from app.db import Database

from .facts import Facts

ExclusionKind = Literal["hex", "registration", "type_code", "operator", "callsign"]


class ExclusionIn(BaseModel):
    filter_id: int | None = None
    kind: ExclusionKind
    value: str = Field(min_length=1, max_length=100)
    until: float | None = None
    note: str = Field("", max_length=200)


class Exclusion(ExclusionIn):
    id: int
    created_at: float

    def active(self, now: float) -> bool:
        return self.until is None or now < self.until

    def matches(self, facts: Facts) -> bool:
        want = self.value.strip().upper()
        if self.kind == "hex":
            return facts.hex.upper() == want
        if self.kind == "operator":
            have = facts.get("operator")
            return bool(have) and want in str(have).upper()
        if self.kind == "callsign":
            have = facts.get("callsign")
            return bool(have) and fnmatch.fnmatchcase(str(have).upper(), want)
        have = facts.get(self.kind)
        return bool(have) and str(have).upper() == want


def is_excluded(exclusions: list[Exclusion], filter_id: int, facts: Facts, now: float) -> bool:
    return any(
        e.active(now) and (e.filter_id is None or e.filter_id == filter_id) and e.matches(facts)
        for e in exclusions
    )


class ExclusionRepo:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    async def all(self) -> list[Exclusion]:
        rows = await self.db.fetchall("SELECT * FROM exclusions ORDER BY created_at DESC")
        return [Exclusion.model_validate(dict(r)) for r in rows]

    async def create(self, body: ExclusionIn) -> Exclusion:
        now = self.clock.now()
        eid = await self.db.insert(
            "INSERT INTO exclusions(filter_id, kind, value, until, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (body.filter_id, body.kind, body.value.strip(), body.until, body.note, now),
        )
        return Exclusion(id=eid, created_at=now, **body.model_dump())

    async def delete(self, exclusion_id: int) -> bool:
        return await self.db.execute("DELETE FROM exclusions WHERE id = ?", (exclusion_id,)) > 0

    async def prune(self) -> None:
        await self.db.execute(
            "DELETE FROM exclusions WHERE until IS NOT NULL AND until < ?", (self.clock.now(),)
        )
