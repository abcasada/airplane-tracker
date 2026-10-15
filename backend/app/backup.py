"""Backup and restore of everything you configured: places, filters, mutes, settings and military
overrides. Credentials (HA token, password, 2FA) are never included.

Restore merges into the current install and is safe to repeat: places that already exist (same
name, kind and position) and filters whose name already exists are reused instead of duplicated.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app import __version__
from app.alerts.manager import AlertingSettings
from app.classify.military import ClassifierSettings
from app.clock import Clock
from app.db import Database
from app.filters.exclusions import ExclusionIn, ExclusionRepo
from app.filters.facts import ANY_VIEWPOINT
from app.filters.models import Condition, FilterDefinition, FilterIn, Group
from app.filters.repo import FilterRepo
from app.places.models import Place, PlaceIn
from app.places.repo import PlaceRepo
from app.predict.engine import PredictionSettings

FORMAT = "airplane-tracker-backup/1"
SETTINGS: dict[str, type[BaseModel]] = {
    "alerting": AlertingSettings,
    "prediction": PredictionSettings,
    "classifier": ClassifierSettings,
}


class BackupPlace(PlaceIn):
    id: int


class BackupFilter(BaseModel):
    id: int
    name: str = Field(min_length=1, max_length=80)
    description: str = ""
    enabled: bool = True
    mode: str = Field("live", pattern="^(live|shadow)$")
    definition: FilterDefinition


class BackupOverride(BaseModel):
    key: str = Field(max_length=120, pattern=r"^(hex|reg):.+")
    is_military: bool
    note: str | None = None
    created_at: float = 0


class Backup(BaseModel):
    format: str = Field(pattern=f"^{FORMAT}$")
    places: list[BackupPlace] = Field(default_factory=list, max_length=500)
    filters: list[BackupFilter] = Field(default_factory=list, max_length=500)
    exclusions: list[ExclusionIn] = Field(default_factory=list, max_length=5000)
    settings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    military_overrides: list[BackupOverride] = Field(default_factory=list, max_length=20000)


async def export(db: Database, clock: Clock) -> dict[str, Any]:
    places = await PlaceRepo(db, clock).all()
    filters = await FilterRepo(db, clock).all()
    exclusions = await ExclusionRepo(db, clock).all()
    overrides = await db.fetchall(
        "SELECT key, is_military, note, created_at FROM military_overrides"
    )
    settings = {}
    for key, model in SETTINGS.items():
        settings[key] = model.model_validate(await db.get_json(key, {})).model_dump()
    return {
        "format": FORMAT,
        "app_version": __version__,
        "exported_at": clock.now(),
        "places": [p.model_dump(exclude={"created_at", "updated_at"}) for p in places],
        "filters": [
            {
                "id": f.id,
                "name": f.name,
                "description": f.description,
                "enabled": f.enabled,
                "mode": f.mode,
                "definition": f.definition.model_dump(),
            }
            for f in filters
        ],
        "exclusions": [
            e.model_dump(exclude={"id", "created_at"})
            for e in exclusions
            if e.until is None or e.until > clock.now()
        ],
        "settings": settings,
        "military_overrides": [
            {
                "key": r["key"],
                "is_military": bool(r["is_military"]),
                "note": r["note"],
                "created_at": r["created_at"],
            }
            for r in overrides
        ],
    }


def _same_place(a: Place, b: PlaceIn) -> bool:
    return (
        a.name == b.name and a.role == b.role
        and abs(a.lat - b.lat) < 1e-4 and abs(a.lon - b.lon) < 1e-4
    )  # fmt: skip


def remap_places(d: FilterDefinition, ids: dict[int, int]) -> FilterDefinition:
    """Point a filter's place references at the restored places' new ids."""

    def walk(node: Condition | Group) -> Condition | Group:
        if isinstance(node, Group):
            return node.model_copy(update={"children": [walk(c) for c in node.children]})
        if node.place is None or node.place == ANY_VIEWPOINT:
            return node
        return node.model_copy(update={"place": ids.get(node.place, node.place)})

    display = d.display_place_id
    if display is not None and display != ANY_VIEWPOINT:
        display = ids.get(display, display)
    root = walk(d.root)
    assert isinstance(root, Group)
    return d.model_copy(update={"root": root, "display_place_id": display})


async def restore(
    db: Database, clock: Clock, b: Backup, include_settings: bool = True
) -> dict[str, int]:
    counts = dict.fromkeys(
        ("places", "places_existing", "filters", "filters_existing", "exclusions", "overrides",
         "settings"), 0,
    )  # fmt: skip
    place_repo = PlaceRepo(db, clock)
    existing = await place_repo.all()
    has_home = any(p.is_home for p in existing)
    place_ids: dict[int, int] = {}
    for bp in b.places:
        data = PlaceIn.model_validate(bp.model_dump(exclude={"id"}))
        match = next((p for p in existing if _same_place(p, data)), None)
        if match is not None:
            place_ids[bp.id] = match.id
            counts["places_existing"] += 1
            continue
        if data.is_home and has_home:
            data = data.model_copy(update={"is_home": False})  # keep this install's home
        created = await place_repo.create(data)
        has_home = has_home or created.is_home
        place_ids[bp.id] = created.id
        counts["places"] += 1

    filter_repo = FilterRepo(db, clock)
    names = {f.name: f.id for f in await filter_repo.all()}
    filter_ids: dict[int, int] = {}
    for bf in b.filters:
        if bf.name in names:
            filter_ids[bf.id] = names[bf.name]
            counts["filters_existing"] += 1
            continue
        created_f = await filter_repo.create(
            FilterIn(
                name=bf.name, description=bf.description, enabled=bf.enabled,
                mode="live" if bf.mode == "live" else "shadow",
                definition=remap_places(bf.definition, place_ids), note="Restored from backup",
            )
        )  # fmt: skip
        names[bf.name] = filter_ids[bf.id] = created_f.id
        counts["filters"] += 1

    excl_repo = ExclusionRepo(db, clock)
    current = {(e.filter_id, e.kind, e.value) for e in await excl_repo.all()}
    for e in b.exclusions:
        if e.until is not None and e.until <= clock.now():
            continue
        if e.filter_id is not None and e.filter_id not in filter_ids:
            continue  # its filter wasn't in the backup
        fid = filter_ids[e.filter_id] if e.filter_id is not None else None
        if (fid, e.kind, e.value) in current:
            continue
        await excl_repo.create(e.model_copy(update={"filter_id": fid}))
        current.add((fid, e.kind, e.value))
        counts["exclusions"] += 1

    for o in b.military_overrides:
        await db.execute(
            "INSERT INTO military_overrides(key, is_military, note, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "is_military = excluded.is_military, note = excluded.note",
            (o.key, int(o.is_military), o.note, o.created_at or clock.now()),
        )
        counts["overrides"] += 1

    if include_settings:
        for key, model in SETTINGS.items():
            if key in b.settings:
                await db.set_json(key, model.model_validate(b.settings[key]).model_dump())
                counts["settings"] += 1
    return counts
