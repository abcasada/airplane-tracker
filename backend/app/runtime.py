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
from app.enrich.hexdb import MIN_INTERVAL_S, HexdbService
from app.enrich.routes import Route, RouteService
from app.filters.engine import CompiledFilter
from app.filters.exclusions import Exclusion, ExclusionIn, ExclusionRepo, is_excluded
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
from app.history import SightingsBook, day_of
from app.ingest.fr24 import (
    GAP_FILL_TICK_S,
    LOST_AFTER_S,
    MAX_GAP_TARGETS,
    GapFiller,
    GapTarget,
    LookupRefusedError,
    LookupResult,
    build_fr24,
    matched_by,
)
from app.ingest.planner import PlannerConfig, QueryCircle, plan
from app.ingest.poller import Poller, SourcePrefs
from app.ingest.recorder import Recorder
from app.ingest.sourcestats import SourceStats
from app.ingest.swim import SwimFeed, swim_config
from app.ingest.swimstore import FiledFlight, SwimFlights
from app.live.broker import Broker
from app.notify.actions import MUTE_SECONDS
from app.notify.base import Notifier
from app.notify.ntfy import NtfyClient, NtfyNotifier
from app.places.models import Place
from app.places.nearby import AirportIndex
from app.places.repo import PlaceRepo
from app.predict.arrival import Runway, runways_for
from app.predict.engine import MIN_CYCLE_S, PredictionSettings, PredictionState, Predictor
from app.replay.backtest import Backtester
from app.sources.base import SourceBatch
from app.sources.registry import build_sources
from app.sources.swim import NAME as SWIM_NAME
from app.sources.swim import normalize_acid
from app.tracks import events as flight_events
from app.tracks.store import Track, TrackStore
from app.watch import Watchlist

from .coverage import Coverage

log = logging.getLogger(__name__)

DATASET_CHECK_INTERVAL_S = 6 * 3600
LIVE_PUBLISH_MIN_INTERVAL_S = 2.0
# Receivers are read every second; recording every 5th second is plenty for replay.
RECEIVER_RECORD_EVERY_S = 5.0
FEED_EVAL_EVERY_S = 0.9
FR24_CIRCLE = QueryCircle("fr24", 0.0, 0.0, 0.0, GAP_FILL_TICK_S, "fr24")
# How long FR24 keeps filling gaps for an aircraft that matched a filter before it was lost.
FR24_FOLLOW_MATCHED_S = 1800.0
SWIM_FLUSH_INTERVAL_S = 30.0
SWIM_PRUNE_INTERVAL_S = 3600.0


class Runtime:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.places: list[Place] = []
        self.store = TrackStore()
        self.enricher = Enricher(ctx.db, ctx.clock)
        self.datasets = DatasetManager(ctx.db, ctx.settings.data_dir, ctx.http, ctx.clock)
        self.hexdb = HexdbService(ctx.db, ctx.http, ctx.clock, ctx.settings.hexdb_enabled)
        self.recorder = Recorder(ctx.settings.data_dir / "recordings")
        self.broker = Broker()
        self.routes = RouteService(ctx.db, ctx.http, ctx.clock)
        self.runways: dict[int, list[Runway]] = {}
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
        self.predictor = Predictor(min_cycle_s=MIN_CYCLE_S)
        self.backtester = Backtester(self)
        self.sightings = SightingsBook(ctx.db)
        self.planner_cfg = PlannerConfig(outer_enabled=True)
        self.watch = Watchlist(ctx.db)
        self.airport_index = AirportIndex()
        self.source_stats = SourceStats()
        self.poller = Poller(
            build_sources(ctx.settings, ctx.http, ctx.clock),
            ctx.clock,
            self.circles,
            self.on_batch,
            watch=self.watch,
        )
        self.fr24 = build_fr24(ctx.settings, ctx.http, ctx.clock, ctx.db)
        self.gap_filler = GapFiller()
        # hex → (registration, callsign, last matched) for matched aircraft the free feeds lost
        self.lost_matched: dict[str, tuple[str | None, str | None, float]] = {}
        self.swim_flights = SwimFlights(ctx.db)
        swim_cfg = swim_config(ctx.settings)
        self.swim = (
            SwimFeed(swim_cfg, ctx.clock, self.swim_flights, self.on_swim_flights)
            if swim_cfg is not None
            else None
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
        await self.reload_prediction()
        await self.reload_filters()
        await self.enricher.reload()
        await self.routes.load()
        await self.airport_index.load(self.ctx.db)
        await self.watch.refresh(self.filters)
        await self.alerts.load()
        self.source_stats.load(await self.ctx.db.get_json("source_stats", {}))
        if self.fr24 is not None:
            await self.fr24.load()
        await self.reload_sources()
        if self.swim is not None:
            await self.swim_flights.load(self.ctx.clock.now())
            self.update_swim_interest()
        if self.ctx.settings.run_background:
            self._spawn(self.poller.run(), "poller")
            if self.fr24 is not None:
                self._spawn(self._fr24_loop(), "fr24")
            if self.swim is not None:
                self._spawn(self.swim.run(), "swim")
                self._spawn(self._swim_maintenance(), "swim-store")
            self._spawn(self._route_loop(), "routes")
            self._spawn(self._hexdb_loop(), "hexdb")
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
        await self.backtester.cancel_all()
        try:
            await self.sightings.flush(self.ctx.clock.now())
            await self.swim_flights.flush()
        except Exception:
            log.exception("could not save sightings or SWIM flights")
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _route_loop(self) -> None:
        while True:
            try:
                if await self.routes.flush():
                    for t in self.store.values():
                        t.route = self.route_for(t)
            except Exception:
                log.exception("route lookup failed")
            await asyncio.sleep(5)

    async def _hexdb_loop(self) -> None:
        while True:
            try:
                await self.hexdb_step()
            except Exception:
                log.exception("hexdb.io lookup failed")
            await asyncio.sleep(MIN_INTERVAL_S)

    async def hexdb_step(self) -> None:
        """One queued hexdb.io lookup; re-enrich the aircraft when an answer arrives."""
        hex_ = await self.hexdb.flush(lambda h: self.store.get(h) is not None)
        track = self.store.get(hex_) if hex_ else None
        if track is not None:
            self.enricher.forget(track.hex)
            track.info = await self.enricher.lookup(track.hex)
            self.classify_track(track)

    def want_hexdb(self, track: Track) -> None:
        """Queue a hexdb.io lookup when registration, type or operator is still unknown."""
        info = track.info
        if info is None or info.hexdb_checked:
            return
        pa = info.plane_alert
        no_operator = not (track.owner_op or track.airline or (pa and pa.operator))
        if not track.registration or not track.type_code or no_operator:
            self.hexdb.want(track.hex)

    async def _maintenance(self) -> None:
        while True:
            try:
                if await self.datasets.refresh_due():
                    await self.refresh_enrichment()
                self.recorder.prune(self.ctx.clock.now())
                await ExclusionRepo(self.ctx.db, self.ctx.clock).prune()
                await self.sightings.prune(
                    self.ctx.clock.now(), self.alerts.tz, {p.id for p in self.places}
                )
                await self.ctx.db.set_json("coverage", self.coverage.to_json())
            except Exception:
                log.exception("maintenance failed")
            await asyncio.sleep(DATASET_CHECK_INTERVAL_S)

    async def _swim_maintenance(self) -> None:
        last_prune = 0.0
        while True:
            await asyncio.sleep(SWIM_FLUSH_INTERVAL_S)
            try:
                await self.swim_flights.flush()
                now = self.ctx.clock.now()
                if now - last_prune >= SWIM_PRUNE_INTERVAL_S:
                    last_prune = now
                    await self.swim_flights.prune(now)
            except Exception:
                log.exception("SWIM store maintenance failed")

    # ---- configuration reloads ---------------------------------------------------------------

    async def reload_places(self) -> None:
        self.places = await PlaceRepo(self.ctx.db, self.ctx.clock).all()
        self.coverage.load(await self.ctx.db.get_json("coverage", {}))
        await self.load_runways()
        self._compile()
        self.update_swim_interest()

    async def load_runways(self) -> None:
        out: dict[int, list[Runway]] = {}
        for p in self.places:
            if p.role != "airport" or not p.airport_icao:
                continue
            rows = await self.ctx.db.fetchall(
                "SELECT r.* FROM runways r JOIN airports a ON a.ident = r.airport_ident "
                "WHERE a.ident = ? OR a.icao = ?",
                (p.airport_icao, p.airport_icao),
            )
            out[p.id] = runways_for([dict(r) for r in rows])
        self.runways = out

    async def reload_filters(self) -> None:
        self.filters = await FilterRepo(self.ctx.db, self.ctx.clock).all()
        self.exclusions = await ExclusionRepo(self.ctx.db, self.ctx.clock).all()
        self._compile()
        await self.watch.refresh(self.filters)
        self.update_swim_interest()

    async def reload_prediction(self) -> None:
        s = PredictionSettings.model_validate(await self.ctx.db.get_json("prediction", {}))
        self.predictor.settings = s
        # The outer polling ring only matters when predicting where aircraft will be.
        self.planner_cfg = PlannerConfig(outer_enabled=s.enabled, horizon_min=s.horizon_s / 60.0)

    async def reload_alerting(self) -> None:
        self.alerts.settings = AlertingSettings.model_validate(
            await self.ctx.db.get_json("alerting", {})
        )

    async def reload_sources(self) -> None:
        prefs = SourcePrefs.model_validate(await self.ctx.db.get_json("sources", {}))
        self.poller.set_disabled(prefs.disabled)
        if self.fr24 is not None:
            self.fr24.enabled = self.fr24.name not in prefs.disabled
        if self.swim is not None:
            self.swim.set_enabled(SWIM_NAME not in prefs.disabled)

    def source_status(self) -> list[dict[str, Any]]:
        """Every configured source: the polled ones, the lookup-only paid ones, then SWIM."""
        return (
            self.poller.status()
            + ([self.fr24.status()] if self.fr24 is not None else [])
            + ([self.swim.status()] if self.swim is not None else [])
        )

    def source_names(self) -> set[str]:
        names = {s.name for s in self.poller.slots}
        if self.fr24 is not None:
            names.add(self.fr24.name)
        if self.swim is not None:
            names.add(self.swim.name)
        return names

    # ---- Flightradar24 (paid): on demand and gap-fill only -----------------------------------

    async def _fr24_loop(self) -> None:
        while True:
            try:
                await self.fr24_gap_fill()
            except Exception:
                log.exception("fr24 gap-fill failed")
            await asyncio.sleep(GAP_FILL_TICK_S)

    @staticmethod
    def _free_heard(t: Track | None, now: float) -> bool:
        """Whether a free source (anything but FR24) heard it recently."""
        return t is not None and any(
            s != "fr24" and now - ts <= LOST_AFTER_S for s, ts in t.heard_by.items()
        )

    async def gap_targets(self, now: float) -> list[GapTarget]:
        """Filter candidates the free feeds lost: followed aircraft (`watch.py`), and aircraft
        that matched a filter in the last 30 min."""
        out: list[GapTarget] = []
        watched: set[str] = set()
        for e in self.watch.entries.values():
            watched |= e.hexes
            tracks = [t for h in sorted(e.hexes) if (t := self.store.get(h)) is not None]
            if any(self._free_heard(t, now) for t in tracks):
                continue
            reg = e.value if e.kind == "reg" else None
            for h in sorted(e.hexes):
                if reg:
                    break
                known = self.store.get(h)
                reg = known.registration if known else None
                reg = reg or (await self.enricher.lookup(h)).registration
            cs = max(tracks, key=lambda t: t.last_seen).callsign if tracks else None
            out.append(GapTarget(f"watch:{e.kind}:{e.value}", reg, cs))
        for hex_ in self.matches:
            t = self.store.get(hex_)
            if t is not None and (t.registration or t.callsign):
                self.lost_matched[hex_] = (t.registration, t.callsign, now)
        while len(self.lost_matched) > MAX_GAP_TARGETS:
            del self.lost_matched[min(self.lost_matched, key=lambda h: self.lost_matched[h][2])]
        for hex_, (reg, cs, at) in list(self.lost_matched.items()):
            if now - at > FR24_FOLLOW_MATCHED_S:
                del self.lost_matched[hex_]
            elif hex_ not in watched and not self._free_heard(self.store.get(hex_), now):
                out.append(GapTarget(f"hex:{hex_}", reg, cs))
        return out[:MAX_GAP_TARGETS]

    async def fr24_gap_fill(self) -> int:
        """One gap-fill round: a single FR24 request for the lost candidates that are due.
        Returns the number of positions it added."""
        svc = self.fr24
        if svc is None or not svc.enabled or not svc.gap_fill_enabled:
            return 0
        now = self.ctx.clock.now()
        due = self.gap_filler.due(await self.gap_targets(now), now)
        if not due:
            return 0
        regs = [t.registration for t in due if t.registration]
        css = [t.callsign for t in due if not t.registration and t.callsign]
        try:
            res = await svc.lookup(regs, css, "gap_fill")
        except LookupRefusedError as e:
            for t in due:  # cap, pace, rate or an error: not a miss, just later
                self.gap_filler.wait(t, now, max(e.retry_in_s or 0.0, 600.0))
            return 0
        for t in due:
            self.gap_filler.done(t, matched_by(res.flights, t), now)
        return await self.ingest_fr24(res)

    async def ingest_fr24(self, res: LookupResult) -> int:
        batch = res.batch
        for o in batch.observations:
            self.watch.learn(o.hex, o.registration, batch.fetched_at)
        if batch.observations:
            await self.on_batch(batch, FR24_CIRCLE)
        return len(batch.observations)

    def update_swim_interest(self) -> None:
        """SWIM keeps flights from/to your airport places and those of followed aircraft."""
        if self.swim is None:
            return
        airports = {p.airport_icao for p in self.places if p.role == "airport" and p.airport_icao}
        acids = {
            a for e in self.watch.entries.values()
            if e.kind == "reg" and (a := normalize_acid(e.value))
        }  # fmt: skip
        self.swim.set_interest(airports, acids)

    async def on_swim_flights(self, flights: list[FiledFlight]) -> None:
        """New or changed flight plans: refresh the route of aircraft flying them."""
        acids = {f.acid for f in flights}
        for t in self.store.values():
            if normalize_acid(t.callsign) in acids or normalize_acid(t.registration) in acids:
                t.route = self.route_for(t)

    def swim_extra(self) -> SwimFlights | None:
        """Filed flights for the `filed_*` filter fields; None when SWIM isn't configured."""
        return self.swim_flights if self.swim is not None else None

    def route_for(self, t: Track) -> Route | None:
        """A filed FAA flight plan first, then the callsign route databases."""
        if self.swim is not None:
            f = self.swim_flights.best_for(t.callsign, t.registration, t.last_seen)
            if f is not None and (route := self.swim_flights.route_for(f)) is not None:
                return route
        return self.routes.get(t.callsign)

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
        from app.api.settings import load_ntfy_config

        cfg = await load_ntfy_config(self.ctx)
        if cfg is None:
            return None
        return NtfyNotifier(
            NtfyClient(self.ctx.http, cfg), self.alerts.tz, self.ctx.secret_key,
            self.ctx.settings.public_url, self.ctx.clock.now,
        )  # fmt: skip

    async def handle_notification_action(self, kind: str, value: Any) -> None:
        """A button tapped on a phone notification (see app.notify.actions)."""
        now = self.ctx.clock.now()
        if kind == "mute":
            await ExclusionRepo(self.ctx.db, self.ctx.clock).create(
                ExclusionIn(
                    kind="hex", value=str(value), until=now + MUTE_SECONDS, note="from notification"
                )
            )
            await self.reload_filters()
        elif kind == "down":
            ids = [int(i) for i in value]
            marks = ",".join("?" for _ in ids)
            await self.ctx.db.execute(
                f"UPDATE alerts SET feedback = 'down' WHERE id IN ({marks})",  # noqa: S608
                ids,
            )
        await self.ctx.db.audit(now, "phone", f"notification.{kind}", {"value": value})

    # ---- pipeline ----------------------------------------------------------------------------

    def override_for(self, t: Track) -> bool | None:
        override = self.overrides.get(f"hex:{t.hex}")
        if override is None and t.registration:
            override = self.overrides.get(f"reg:{t.registration.upper()}")
        return override

    def classifier_inputs(self, t: Track, override: bool | None) -> Inputs:
        info = t.info
        pa = info.plane_alert if info else None
        stats = self.enricher.type_info(t.type_code)
        return Inputs(
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
            registrant_type=info.faa.registrant_type if info and info.faa else None,
        )

    def classify_track(self, t: Track) -> None:
        t.classification = classify(
            self.classifier_inputs(t, self.override_for(t)), self.classifier
        )

    async def refresh_enrichment(self) -> None:
        """After a dataset import: reload lookups and re-enrich every tracked aircraft."""
        await self.enricher.reload()
        await self.load_runways()
        await self.airport_index.load(self.ctx.db)
        await self.watch.refresh(self.filters)  # registrations may resolve now
        for t in self.store.values():
            t.info = await self.enricher.lookup(t.hex)
            t.airline = self.enricher.airline_for(t.callsign)
            t.airline_callsign = t.callsign
        self._reclassify_all()

    def _reclassify_all(self) -> None:
        for t in self.store.values():
            self.classify_track(t)

    async def ingest(self, batch: SourceBatch, circle: QueryCircle | None = None) -> None:
        # Lookups of followed aircraft cover the whole world, so they'd skew the Sources report;
        # it counts only positions inside the polled circles, where every source was asked.
        counted = self.poller.last_circles if circle is not None and circle.tier != "watch" else []
        now = self.ctx.clock.now()
        for obs in batch.observations:
            if counted and obs.lat is not None and obs.lon is not None:
                lat, lon = obs.lat, obs.lon
                if any(geo.haversine_nm(c.lat, c.lon, lat, lon) <= c.radius_nm for c in counted):
                    self.source_stats.add(obs.source, obs.hex, now)
            track, is_new = self.store.update(obs)
            await self.enrich_track(track, is_new)
            if is_new:
                self.want_hexdb(track)
            if track.route is None or track.route.source != "faa-swim":
                self.routes.want(track.callsign, track.lat, track.lon)
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

    async def enrich_track(self, track: Track, is_new: bool) -> None:
        """Enrichment and classification. Shared with replay, so backtests see the same facts."""
        callsign_changed = track.airline_callsign != track.callsign
        if is_new or track.info is None:
            track.info = await self.enricher.lookup(track.hex)
        if callsign_changed:
            track.airline = self.enricher.airline_for(track.callsign)
            track.airline_callsign = track.callsign
        if is_new or callsign_changed or track.classification is None:
            self.classify_track(track)
        track.route = self.route_for(track)

    async def on_batch(self, batch: SourceBatch, circle: QueryCircle) -> None:
        async with self._lock:
            await self.ingest(batch, circle)
            every = RECEIVER_RECORD_EVERY_S if circle.tier == "feed" else 0.0
            if batch.source != "fr24":  # FR24's terms: no storing its data beyond the cache
                await asyncio.to_thread(self.recorder.write, batch, every)
            if self.source_stats.dirty:
                self.source_stats.dirty = False
                await self.ctx.db.set_json("source_stats", self.source_stats.to_json())
            now = self.ctx.clock.now()
            last = self.last_eval_at
            if circle.tier == "feed" and last is not None and 0 <= now - last < FEED_EVAL_EVERY_S:
                return  # a receiver and its UAT twin both arrive every second: evaluate once
            # Before expiry, so an aircraft lost low near an airfield still counts as landed.
            events = flight_events.update(self.store.values(), self.airport_index, now)
            self.store.expire(now)
            await self.evaluate(events)

    def home(self) -> Place | None:
        return next((p for p in self.places if p.is_home), None)

    def eval_context(self, now: float | None = None) -> EvalContext:
        tz: ZoneInfo = self.alerts.tz
        extras = {
            "runways": self.runways, "sightings": self.sightings, "airports": self.airport_index,
            "swim": self.swim_extra(),
        }  # fmt: skip
        return EvalContext(now or self.ctx.clock.now(), self.places_by_id(), tz, extras)

    def facts_for_all(self, ctx: EvalContext) -> list[LiveFacts]:
        return [LiveFacts(t, ctx) for t in self.store.values()]

    async def evaluate(
        self, events: list[tuple[Track, flight_events.FlightEvent]] | None = None
    ) -> None:
        started = time.perf_counter()
        now = self.ctx.clock.now()
        ectx = self.eval_context(now)
        tracks = self.store.values()
        self.sightings.record(tracks, [p for p in self.places if p.is_active(now)], now, ectx.tz)
        await self.sightings.prepare(tracks, day_of(now, ectx.tz))
        await self.sightings.maybe_flush(now)
        self.predictor.update(tracks, self.places, now, self.runways)
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
        for hex_ in by_hex:
            self.hexdb.prioritize(hex_)
        await self.alerts.process(
            now, matches, ectx.places, [(LiveFacts(t, ectx), ev) for t, ev in events or []]
        )
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
        "sources": t.sources(f.ctx.now),
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
        "predictions": _active_predictions(f),
        "path": _path(f) if filter_ids or _active_predictions(f) else None,
    }


def _active_predictions(f: LiveFacts) -> list[dict[str, Any]]:
    state: PredictionState | None = f.track.extra.get("prediction")
    if state is None:
        return []
    out = []
    for pid, pred in state.by_place.items():
        if pred.active and pid in f.ctx.places:
            out.append(
                {"place_id": pid, "place": f.ctx.places[pid].name, "kind": pred.kind,
                 "eta_s": pred.eta_s, "confidence": pred.confidence}
            )  # fmt: skip
    return out


def _path(f: LiveFacts) -> list[tuple[float, float]] | None:
    state: PredictionState | None = f.track.extra.get("prediction")
    return state.path if state and state.path else None
