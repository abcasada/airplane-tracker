"""Replay recordings through the live runtime's settings: prediction metrics plus per-filter
episode counts. Used by the CLI and the replay-fixture tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.filters.engine import CompiledFilter
from app.history import SightingsBook
from app.ingest.recorder import read_batches
from app.predict.engine import PredictionSettings, Predictor

from .backtest import BacktestEngine, BacktestSpec
from .core import Replay
from .metrics import MetricsCollector

if TYPE_CHECKING:
    from app.runtime import Runtime


async def evaluate_recordings(
    rt: Runtime,
    root: Path,
    start: float,
    end: float,
    prediction: PredictionSettings | None = None,
    seed: int = 7,
) -> dict[str, Any]:
    settings = prediction or rt.predictor.settings
    replay = Replay(
        rt.places,
        enrich=rt.enrich_track,
        predictor=Predictor(settings, seed, paths=False),
        runways=rt.runways,
        tz=rt.alerts.tz,
        history=SightingsBook(rt.ctx.db, read_only=True),
        extras={"airports": rt.airport_index},
    )
    metrics = MetricsCollector(rt.places, settings.lead_max_s)
    places = rt.places_by_id()
    engines: dict[int, tuple[str, BacktestEngine]] = {}
    for f in rt.filters:
        compiled = CompiledFilter(f.definition, places)
        if f.enabled and compiled.valid:
            spec = BacktestSpec(f.definition, None, f.id, f.mode, start, end)
            engine = BacktestEngine(
                spec, compiled, None, rt.exclusions, rt.alerts.settings, rt.alerts.tz, end
            )
            engines[f.id] = (f.name, engine)
    batches = 0
    for fetched_at, observations in read_batches(root, start, end):
        cycle = await replay.step(fetched_at, observations)
        metrics.observe(cycle)
        for _, engine in engines.values():
            engine.observe(cycle)
        batches += 1
    filters = []
    for fid, (name, engine) in engines.items():
        r = engine.result()
        filters.append({"id": fid, "name": name, **(r["totals"]["draft"] or {}),
                        "aircraft_list": r["only_draft"]})  # fmt: skip
    return {"batches": batches, "prediction": metrics.report(), "filters": filters}
