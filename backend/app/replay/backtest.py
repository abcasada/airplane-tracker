"""What-if backtest: run a draft filter and its saved version over recorded days.

Uses the same replay, filter engine, exclusions and (opt-in) suppression rules as live alerting,
and counts episodes the way AlertManager does: one per aircraft while it keeps matching.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from app.alerts.manager import AlertingSettings, suppression_reason
from app.filters.engine import CompiledFilter
from app.filters.exclusions import Exclusion, is_excluded
from app.filters.facts import LiveFacts
from app.filters.fields import FIELDS
from app.filters.models import FilterDefinition
from app.ingest.recorder import read_batches
from app.predict.engine import Predictor
from app.sources.base import Observation

from .core import Cycle, Replay

if TYPE_CHECKING:
    from app.runtime import Runtime

log = logging.getLogger(__name__)

MAX_LISTED = 100
CHUNK_BATCHES = 50
KEEP_JOBS = 4


def uses_prediction(*compiled: CompiledFilter) -> bool:
    return any(FIELDS[k].group == "Prediction" for c in compiled for k, _ in c.referenced()
               if k in FIELDS)  # fmt: skip


@dataclass
class _Side:
    compiled: CompiledFilter
    definition: FilterDefinition
    mode: str
    filter_id: int
    active: dict[str, float] = field(default_factory=dict)  # hex → last matched
    last_notified: dict[str, float] = field(default_factory=dict)
    episodes_by_day: dict[str, int] = field(default_factory=dict)
    notified_by_day: dict[str, int] = field(default_factory=dict)
    aircraft: dict[str, dict[str, Any]] = field(default_factory=dict)

    def summary_for(self, day: str) -> dict[str, int]:
        return {
            "episodes": self.episodes_by_day.get(day, 0),
            "notified": self.notified_by_day.get(day, 0),
        }


@dataclass
class BacktestSpec:
    draft: FilterDefinition
    saved: FilterDefinition | None
    filter_id: int  # 0 for a filter that hasn't been saved yet
    mode: str
    start: float
    end: float


class BacktestEngine:
    def __init__(
        self,
        spec: BacktestSpec,
        compiled_draft: CompiledFilter,
        compiled_saved: CompiledFilter | None,
        exclusions: list[Exclusion],
        alerting: AlertingSettings,
        tz: ZoneInfo,
        real_now: float,
    ) -> None:
        self.spec = spec
        self.exclusions = exclusions
        self.alerting = alerting
        self.tz = tz
        self.real_now = real_now
        self.sides: dict[str, _Side] = {
            "draft": _Side(compiled_draft, spec.draft, spec.mode, spec.filter_id)
        }
        if compiled_saved is not None and spec.saved is not None:
            self.sides["saved"] = _Side(compiled_saved, spec.saved, spec.mode, spec.filter_id)
        self.days: list[str] = []
        self.batches = 0

    def _day(self, now: float) -> str:
        return datetime.fromtimestamp(now, self.tz).strftime("%Y-%m-%d")

    def observe(self, cycle: Cycle) -> None:
        self.batches += 1
        day = self._day(cycle.now)
        if not self.days or self.days[-1] != day:
            self.days.append(day)
        for side in self.sides.values():
            self._evaluate(side, cycle.now, day, cycle.facts)

    def _evaluate(self, side: _Side, now: float, day: str, facts: list[LiveFacts]) -> None:
        # Exclusions as they stand today, as if they had been in place all along.
        for f in facts:
            if not side.compiled.matches(f):
                continue
            if is_excluded(self.exclusions, side.filter_id, f, self.real_now):
                continue
            hex_ = f.hex
            if hex_ in side.active:
                side.active[hex_] = now
                continue
            side.active[hex_] = now
            side.episodes_by_day[day] = side.episodes_by_day.get(day, 0) + 1
            notified_today = side.notified_by_day.get(day, 0)
            reason = suppression_reason(
                side.mode,
                side.definition.notify,
                self.alerting,
                now,
                self.tz,
                side.last_notified.get(hex_),
                None,  # other filters aren't replayed, so cross-filter suppression can't apply
                notified_today,
            )
            if reason is None:
                side.notified_by_day[day] = side.notified_by_day.get(day, 0) + 1
                side.last_notified[hex_] = now
            a = side.aircraft.get(hex_)
            if a is None:
                t = f.track
                side.aircraft[hex_] = {
                    "hex": hex_, "callsign": t.callsign, "registration": t.registration,
                    "type_code": t.type_code, "type_desc": t.type_desc, "first_at": now,
                    "episodes": 1,
                }  # fmt: skip
            else:
                a["episodes"] += 1
                a["callsign"] = a["callsign"] or f.track.callsign
        end_after = self.alerting.episode_end_after_s
        for hex_, last in list(side.active.items()):
            if now - last >= end_after:
                del side.active[hex_]

    def result(self) -> dict[str, Any]:
        draft = self.sides["draft"]
        saved = self.sides.get("saved")
        days = [
            {
                "date": d,
                "draft": draft.summary_for(d),
                "saved": saved.summary_for(d) if saved else None,
            }
            for d in self.days
        ]

        def total(side: _Side | None) -> dict[str, int] | None:
            if side is None:
                return None
            return {
                "episodes": sum(side.episodes_by_day.values()),
                "notified": sum(side.notified_by_day.values()),
                "aircraft": len(side.aircraft),
            }

        def listed(hexes: set[str], side: _Side) -> list[dict[str, Any]]:
            rows = sorted((side.aircraft[h] for h in hexes), key=lambda a: a["first_at"])
            return rows[:MAX_LISTED]

        out: dict[str, Any] = {
            "days": days,
            "totals": {"draft": total(draft), "saved": total(saved)},
            "batches": self.batches,
        }
        if saved is not None:
            d, s = set(draft.aircraft), set(saved.aircraft)
            out["only_draft"] = listed(d - s, draft)
            out["only_saved"] = listed(s - d, saved)
            out["both"] = len(d & s)
        else:
            out["only_draft"] = listed(set(draft.aircraft), draft)
            out["only_saved"] = []
            out["both"] = 0
        return out


def _chunks(
    root: Path, start: float, end: float
) -> Iterator[list[tuple[float, list[Observation]]]]:
    it = read_batches(root, start, end)
    while chunk := list(itertools.islice(it, CHUNK_BATCHES)):
        yield chunk


@dataclass
class BacktestJob:
    id: str
    started_at: float
    status: Literal["running", "done", "error", "cancelled"] = "running"
    progress: float = 0.0
    result: dict[str, Any] | None = None
    error: str | None = None
    elapsed_s: float = 0.0
    task: asyncio.Task[None] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "progress": round(self.progress, 3),
            "result": self.result,
            "error": self.error,
            "elapsed_s": round(self.elapsed_s, 1),
        }


class Backtester:
    """Runs one backtest at a time as a background task that yields to live work every batch."""

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.jobs: dict[str, BacktestJob] = {}

    def get(self, job_id: str) -> BacktestJob | None:
        return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.task is None or job.task.done():
            return False
        job.task.cancel()
        return True

    async def cancel_all(self) -> None:
        tasks = [j.task for j in self.jobs.values() if j.task is not None and not j.task.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def start(self, spec: BacktestSpec) -> BacktestJob:
        for job in self.jobs.values():
            if job.task is not None and not job.task.done():
                job.task.cancel()
        job = BacktestJob(uuid.uuid4().hex[:12], self.rt.ctx.clock.now())
        job.task = asyncio.create_task(self._run(job, spec), name=f"backtest-{job.id}")
        self.jobs[job.id] = job
        while len(self.jobs) > KEEP_JOBS:
            self.jobs.pop(next(iter(self.jobs)))
        return job

    async def _run(self, job: BacktestJob, spec: BacktestSpec) -> None:
        started = time.perf_counter()
        rt = self.rt
        try:
            places = rt.places_by_id()
            draft = CompiledFilter(spec.draft, places)
            if not draft.valid:
                raise ValueError("; ".join(draft.errors))
            saved = CompiledFilter(spec.saved, places) if spec.saved is not None else None
            predict = uses_prediction(draft, *([saved] if saved else []))
            replay = Replay(
                rt.places,
                enrich=rt.enrich_track,
                predictor=Predictor(rt.predictor.settings, paths=False) if predict else None,
                runways=rt.runways,
                tz=rt.alerts.tz,
            )
            engine = BacktestEngine(
                spec, draft, saved if saved and saved.valid else None,
                rt.exclusions, rt.alerts.settings, rt.alerts.tz, rt.ctx.clock.now(),
            )  # fmt: skip
            span = max(1.0, spec.end - spec.start)
            chunks = _chunks(rt.recorder.root, spec.start, spec.end)
            while chunk := await asyncio.to_thread(next, chunks, None):
                for fetched_at, observations in chunk:
                    engine.observe(await replay.step(fetched_at, observations))
                    job.progress = min(1.0, (fetched_at - spec.start) / span)
                    await asyncio.sleep(0)
                job.elapsed_s = time.perf_counter() - started
            job.result = engine.result() | {"predicted": predict}
            job.status, job.progress = "done", 1.0
        except asyncio.CancelledError:
            job.status = "cancelled"
            raise
        except Exception as e:  # reported to the UI; the live loop is unaffected
            log.exception("backtest failed")
            job.status, job.error = "error", str(e)
        finally:
            job.elapsed_s = time.perf_counter() - started
