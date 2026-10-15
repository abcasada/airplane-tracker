import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.clock import Clock
from app.sources.base import AreaQuery, AreaSource, SourceBatch, SourceError

from .planner import QueryCircle
from .ratelimit import CircuitBreaker, TokenBucket

log = logging.getLogger(__name__)

# Use at most half of each source's published rate limit.
BUDGET_FRACTION = 0.5
IDLE_INTERVAL_S = 60.0
IDLE_AFTER_EMPTY_POLLS = 3

BatchHandler = Callable[[SourceBatch, QueryCircle], Awaitable[None]]


@dataclass
class SourceSlot:
    source: AreaSource
    bucket: TokenBucket
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    requests: int = 0
    errors: int = 0
    day: int = 0
    requests_today: int = 0
    last_latency_ms: float | None = None

    def count(self, now: float) -> None:
        self.requests += 1
        day = int(now // 86400)
        if day != self.day:
            self.day, self.requests_today = day, 0
        self.requests_today += 1


class Poller:
    def __init__(
        self,
        sources: list[AreaSource],
        clock: Clock,
        circles: Callable[[], list[QueryCircle]],
        on_batch: BatchHandler,
    ) -> None:
        self.slots = [
            SourceSlot(s, TokenBucket(s.max_rate_per_s * BUDGET_FRACTION)) for s in sources
        ]
        self.clock = clock
        self.circles = circles
        self.on_batch = on_batch
        self.next_due: dict[str, float] = {}
        self.empty_streak: dict[str, int] = {}
        self.last_circles: list[QueryCircle] = []

    def _interval(self, c: QueryCircle) -> float:
        if self.empty_streak.get(c.key, 0) >= IDLE_AFTER_EMPTY_POLLS:
            return max(c.interval_s, IDLE_INTERVAL_S)
        return c.interval_s

    async def fetch(self, circle: QueryCircle) -> SourceBatch | None:
        query = AreaQuery(circle.lat, circle.lon, circle.radius_nm)
        for slot in self.slots:
            now = self.clock.now()
            if slot.breaker.is_open(now):
                continue
            if not slot.bucket.take(now):
                return None  # primary is healthy but out of budget: wait, don't fail over
            slot.count(now)
            started = time.perf_counter()
            try:
                batch = await slot.source.fetch(query)
            except SourceError as e:
                slot.errors += 1
                slot.breaker.failure(self.clock.now(), str(e), e.is_client_error)
                log.warning("%s failed: %s", slot.source.name, e)
                continue
            slot.last_latency_ms = (time.perf_counter() - started) * 1000
            slot.breaker.success(self.clock.now())
            return batch
        return None

    async def tick(self) -> float:
        """Poll every due circle once. Returns seconds until the next one is due."""
        circles = self.circles()
        self.last_circles = circles
        keys = {c.key for c in circles}
        for k in list(self.next_due):
            if k not in keys:
                del self.next_due[k]
        now = self.clock.now()
        for c in sorted(circles, key=lambda c: self.next_due.get(c.key, 0.0)):
            if self.next_due.get(c.key, 0.0) > now:
                continue
            batch = await self.fetch(c)
            now = self.clock.now()
            if batch is None:
                self.next_due[c.key] = now + 2.0
                continue
            self.empty_streak[c.key] = (
                0 if batch.observations else self.empty_streak.get(c.key, 0) + 1
            )
            self.next_due[c.key] = now + self._interval(c)
            try:
                await self.on_batch(batch, c)
            except Exception:
                log.exception("batch handler failed")
        if not self.next_due:
            return 5.0
        return max(0.0, min(self.next_due.values()) - self.clock.now())

    async def run(self) -> None:
        while True:
            try:
                wait = await self.tick()
            except Exception:
                log.exception("poller tick failed")
                wait = 5.0
            await asyncio.sleep(min(max(wait, 0.25), 2.0))

    def status(self) -> list[dict[str, Any]]:
        now = self.clock.now()
        return [
            {
                "name": s.source.name,
                "healthy": not s.breaker.is_open(now),
                "open_for_s": max(0.0, s.breaker.open_until - now),
                "last_ok_at": s.breaker.last_ok_at,
                "last_error": s.breaker.last_error,
                "requests": s.requests,
                "requests_today": s.requests_today,
                "errors": s.errors,
                "budget_per_s": s.bucket.rate,
                "last_latency_ms": s.last_latency_ms,
            }
            for s in self.slots
        ]
