"""Background services: polling → tracks → enrichment → classification → filters → alerts.

Everything runs as asyncio tasks in the single app process.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from zoneinfo import ZoneInfo

from app import geo
from app.alerts.manager import AlertingSettings, AlertManager, Match
from app.classify.military import ClassifierSettings, Inputs, classify
from app.context import AppContext
from app.enrich.datasets import DatasetManager
from app.enrich.enricher import Enricher
from app.filters.engine import CompiledFilter
from app.filters.exclusions import Exclusion, ExclusionRepo, is_excluded
from app.filters.facts import EvalContext, LiveFacts
from app.filters.models import (
    Condition,
    FilterDefinition,
    FilterIn,
    FilterOut,
    Group,
    NotifySettings,
)
from app.filters.repo import FilterRepo
from app.ingest.planner import PlannerConfig, QueryCircle, plan
from app.ingest.poller import Poller
from app.ingest.recorder import Recorder
from app.live.broker import Broker
from app.notify.base import Notifier
from app.notify.homeassistant import HomeAssistantClient, HomeAssistantNotifier
from app.places.models import Place
from app.places.repo import PlaceRepo
from app.sources import readsb
from app.sources.base import SourceBatch
from app.tracks.store import Track, TrackStore

from .coverage import Coverage

log = logging.getLogger(__name__)

DATASET_CHECK_INTERVAL_S = 6 * 3600
LIVE_PUBLISH_MIN_INTERVAL_S = 2.0


class Runtime:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.places: list[Place] = []
        self.store = TrackStore()
        self.enricher = Enricher(ctx.db)
        self.datasets = DatasetManager(ctx.db, ctx.settings.data_dir, ctx.http, ctx.clock)
        self.recorder = Recorder(ctx.settings.data_dir / "recordings")
        self.broker = Broker()
        self.coverage = Coverage()
        self.classifier = ClassifierSettings()
        self.overrides: dict[str, bool] = {}
        self.filters: list[FilterOut] = []
        self.compiled: dict[int, CompiledFilter] = {}
        self.exclusions: list[Exclusion] = []
        self.alerts = AlertManager(
            ctx.db,
            ctx.clock,
            self._notifier,
            public_url=ctx.settings.public_url,
            on_event=self.broker.publish,
        )
        self.planner_cfg = PlannerConfig()
        self.poller = Poller(
            [readsb.adsbfi(ctx.http, ctx.clock), readsb.adsblol(ctx.http, ctx.clock)],
            ctx.clock,
            self.circles,
            self.on_batch,
        )
        self.matches: dict[str, list[int]] = {}  # hex → matching filter ids (last cycle)
        self.last_eval_ms = 0.0
        self.last_eval_at: float | None = None
        self._last_publish = 0.0
        self._tasks: list[asyncio.Task[None]] = []
        self._lock = asyncio.Lock()

    # ---- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        await self._seed()
        await self.reload_places()
        await self.reload_classifier()
        await self.reload_filters()
        await self.enricher.reload()
        await self.alerts.load()
        if self.ctx.settings.run_background:
            self._spawn(self.poller.run(), "poller")
            self._spawn(self._maintenance(), "maintenance")

    async def _seed(self) -> None:
        """Starter content on first run. It's ordinary data: edit or delete it freely."""
        if await self.ctx.db.get_json("seeded", False):
            return
        await FilterRepo(self.ctx.db, self.ctx.clock).create(
            FilterIn(
                name="Emergency squawks",
                description="Any aircraft in range squawking 7500, 7600 or 7700.",
                definition=FilterDefinition(
                    root=Group(
                        children=[Condition(id="c1", field="emergency_squawk", op="is_true")]
                    ),
                    notify=NotifySettings(priority="critical"),
                ),
                note="Starter filter",
            )
        )
        await self.ctx.db.set_json("seeded", True)

    def _spawn(self, coro: Any, name: str) -> None:
        self._tasks.append(asyncio.create_task(coro, name=name))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _maintenance(self) -> None:
        while True:
            try:
                if await self.datasets.refresh_due():
                    await self.refresh_enrichment()
                self.recorder.prune(self.ctx.clock.now())
                await ExclusionRepo(self.ctx.db, self.ctx.clock).prune()
                await self.ctx.db.set_json("coverage", self.coverage.to_json())
            except Exception:
                log.exception("maintenance failed")
            await asyncio.sleep(DATASET_CHECK_INTERVAL_S)

    # ---- configuration reloads ---------------------------------------------------------------

    async def reload_places(self) -> None:
        self.places = await PlaceRepo(self.ctx.db, self.ctx.clock).all()
        self.coverage.load(await self.ctx.db.get_json("coverage", {}))
        self._compile()

    async def reload_filters(self) -> None:
        self.filters = await FilterRepo(self.ctx.db, self.ctx.clock).all()
        self.exclusions = await ExclusionRepo(self.ctx.db, self.ctx.clock).all()
        self._compile()

    async def reload_alerting(self) -> None:
        self.alerts.settings = AlertingSettings.model_validate(
            await self.ctx.db.get_json("alerting", {})
        )

    async def reload_classifier(self) -> None:
        self.classifier = ClassifierSettings.model_validate(
            await self.ctx.db.get_json("classifier", {})
        )
        rows = await self.ctx.db.fetchall("SELECT key, is_military FROM military_overrides")
        self.overrides = {r["key"]: bool(r["is_military"]) for r in rows}
        self._reclassify_all()

    def _compile(self) -> None:
        places = self.places_by_id()
        self.compiled = {f.id: CompiledFilter(f.definition, places) for f in self.filters}

    def places_by_id(self) -> dict[int, Place]:
        return {p.id: p for p in self.places}

    def active_places(self) -> list[Place]:
        now = self.ctx.clock.now()
        return [p for p in self.places if p.is_active(now)]

    def circles(self) -> list[QueryCircle]:
        return plan(self.places, self.ctx.clock.now(), self.planner_cfg)

    async def _notifier(self) -> Notifier | None:
        from app.api.settings import load_ha_config

        cfg = await load_ha_config(self.ctx)
        return HomeAssistantNotifier(HomeAssistantClient(self.ctx.http, cfg)) if cfg else None

    # ---- pipeline ----------------------------------------------------------------------------

    def classify_track(self, t: Track) -> None:
        info = t.info
        pa = info.plane_alert if info else None
        stats = self.enricher.type_info(t.type_code)
        override = self.overrides.get(f"hex:{t.hex}")
        if override is None and t.registration:
            override = self.overrides.get(f"reg:{t.registration.upper()}")
        t.classification = classify(
            Inputs(
                hex=t.hex,
                callsign=t.callsign,
                registration=t.registration,
                type_code=t.type_code,
                owner_op=t.owner_op,
                db_military=bool(t.api_db_flags & 1) or bool(info and info.db_military),
                in_military_range=bool(info and info.in_military_range),
                plane_alert_cmpg=pa.cmpg if pa else None,
                plane_alert_operator=pa.operator if pa else None,
                type_airframes=stats[0] if stats else 0,
                type_military_share=stats[1] if stats else None,
                callsign_is_airline=t.airline is not None,
                override=override,
            ),
            self.classifier,
        )

    async def refresh_enrichment(self) -> None:
        """After a dataset import: reload lookups and re-enrich every tracked aircraft."""
        await self.enricher.reload()
        for t in self.store.values():
            t.info = await self.enricher.lookup(t.hex)
            t.airline = self.enricher.airline_for(t.callsign)
            t.airline_callsign = t.callsign
        self._reclassify_all()

    def _reclassify_all(self) -> None:
        for t in self.store.values():
            self.classify_track(t)

    async def ingest(self, batch: SourceBatch) -> None:
        for obs in batch.observations:
            track, is_new = self.store.update(obs)
            callsign_changed = track.airline_callsign != track.callsign
            if is_new or track.info is None:
                track.info = await self.enricher.lookup(track.hex)
            if callsign_changed:
                track.airline = self.enricher.airline_for(track.callsign)
                track.airline_callsign = track.callsign
            if is_new or callsign_changed or track.classification is None:
                self.classify_track(track)
            home = self.home()
            if home is not None and track.has_position:
                assert track.lat is not None and track.lon is not None
                self.coverage.add(
                    batch.fetched_at,
                    geo.haversine_nm(home.lat, home.lon, track.lat, track.lon),
                    None if track.on_ground else track.altitude_ft,
                    home.elevation_ft,
                    track.on_ground,
                )

    async def on_batch(self, batch: SourceBatch, circle: QueryCircle) -> None:
        async with self._lock:
            await self.ingest(batch)
            await asyncio.to_thread(self.recorder.write, batch)
            self.store.expire(self.ctx.clock.now())
            await self.evaluate()

    def home(self) -> Place | None:
        return next((p for p in self.places if p.is_home), None)

    def eval_context(self, now: float | None = None) -> EvalContext:
        tz: ZoneInfo = self.alerts.tz
        return EvalContext(now or self.ctx.clock.now(), self.places_by_id(), tz)

    def facts_for_all(self, ctx: EvalContext) -> list[LiveFacts]:
        return [LiveFacts(t, ctx) for t in self.store.values()]

    async def evaluate(self) -> None:
        started = time.perf_counter()
        now = self.ctx.clock.now()
        ectx = self.eval_context(now)
        all_facts = self.facts_for_all(ectx)
        matches: list[Match] = []
        by_hex: dict[str, list[int]] = {}
        for f in self.filters:
            if not f.enabled:
                continue
            compiled = self.compiled.get(f.id)
            if compiled is None or not compiled.valid:
                continue
            for facts in all_facts:
                if compiled.matches(facts) and not is_excluded(self.exclusions, f.id, facts, now):
                    matches.append(Match(f, compiled, facts))
                    by_hex.setdefault(facts.hex, []).append(f.id)
        self.matches = by_hex
        await self.alerts.process(now, matches, ectx.places)
        self.last_eval_ms = (time.perf_counter() - started) * 1000
        self.last_eval_at = now
        self._publish_live(all_facts, now)

    def _publish_live(self, all_facts: list[LiveFacts], now: float) -> None:
        if not self.broker.clients or now - self._last_publish < LIVE_PUBLISH_MIN_INTERVAL_S:
            return
        self._last_publish = now
        self.broker.publish("aircraft", {"now": now, "aircraft": self.summaries(all_facts)})

    def summaries(self, all_facts: list[LiveFacts]) -> list[dict[str, Any]]:
        home = self.home()
        return [aircraft_summary(f, home, self.matches.get(f.hex, [])) for f in all_facts]


def aircraft_summary(f: LiveFacts, home: Place | None, filter_ids: list[int]) -> dict[str, Any]:
    t = f.track
    cls = t.classification
    pa = t.info.plane_alert if t.info else None
    hid = home.id if home else None
    return {
        "hex": t.hex,
        "callsign": t.callsign,
        "registration": t.registration,
        "type_code": t.type_code,
        "type_desc": t.type_desc,
        "operator": f.get("operator"),
        "lat": t.lat,
        "lon": t.lon,
        "altitude_ft": None if t.on_ground else t.altitude_ft,
        "on_ground": t.on_ground,
        "gs_kt": t.gs_kt,
        "track_deg": t.track_deg,
        "vertical_rate_fpm": t.vertical_rate_fpm,
        "squawk": t.squawk,
        "category": t.category,
        "pos_source": t.pos_source,
        "seen_s": round(f.ctx.now - t.last_seen, 1),
        "military": bool(cls and cls.military),
        "military_score": cls.military_score if cls else None,
        "government": bool(cls and cls.government),
        "police": bool(cls and cls.police),
        "plane_alert_category": pa.category if pa else None,
        "distance_nm": f.get("distance_nm", hid) if hid is not None else None,
        "direction": f.get("direction_from_place", hid) if hid is not None else None,
        "elevation_deg": f.get("elevation_angle_deg", hid) if hid is not None else None,
        "visible": f.get("visible_now", hid) if hid is not None else None,
        "filters": filter_ids,
    }
