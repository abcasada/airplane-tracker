"""Feeds recorded batches through the same track store, enrichment, predictor and filter facts
as the live loop, one batch per cycle, on the recording's own clock."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from app.filters.facts import EvalContext, LiveFacts
from app.places.models import Place
from app.predict.arrival import Runway
from app.predict.engine import Predictor
from app.sources.base import Observation
from app.tracks.store import Track, TrackStore

EnrichFn = Callable[[Track, bool], Awaitable[None]]


@dataclass
class Cycle:
    now: float
    facts: list[LiveFacts]


@dataclass
class Replay:
    places: list[Place]
    enrich: EnrichFn | None = None
    predictor: Predictor | None = None
    runways: dict[int, list[Runway]] = field(default_factory=dict)
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))
    store: TrackStore = field(default_factory=TrackStore)

    def __post_init__(self) -> None:
        self._by_id = {p.id: p for p in self.places}
        self._extras: dict[str, Any] = {"runways": self.runways}

    async def step(self, fetched_at: float, observations: list[Observation]) -> Cycle:
        for obs in observations:
            track, is_new = self.store.update(obs)
            if self.enrich is not None:
                await self.enrich(track, is_new)
        self.store.expire(fetched_at)
        tracks = self.store.values()
        if self.predictor is not None:
            self.predictor.update(tracks, self.places, fetched_at, self.runways)
        ctx = EvalContext(fetched_at, self._by_id, self.tz, self._extras)
        return Cycle(fetched_at, [LiveFacts(t, ctx) for t in tracks])
