"""Polls every enabled live source, each within its own budget, token bucket and circuit breaker.

Area sources are asked about every query circle on their own schedule; feed sources (a local
receiver) are read on a fixed interval. Every batch goes to the same handler, where the track
store merges observations by ICAO hex. Lookups of followed aircraft (`watch.py`) still go to the
first healthy lookup source and spend that source's budget.
"""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from app import geo
from app.clock import Clock
from app.sources.base import (
    AreaQuery,
    AreaSource,
    FeedSource,
    LookupSource,
    MeteredSource,
    SourceBatch,
    SourceError,
)
from app.watch import MAX_HEXES_PER_REQUEST, WATCH_INTERVAL_S, Watchlist

from .planner import QueryCircle
from .ratelimit import CircuitBreaker, TokenBucket

log = logging.getLogger(__name__)

# Use at most half of each source's published rate limit.
BUDGET_FRACTION = 0.5
IDLE_INTERVAL_S = 60.0
IDLE_AFTER_EMPTY_POLLS = 3
RETRY_S = 2.0
RATE_WINDOW_S = 600.0
MAX_SLEEP_S = 2.0

BatchHandler = Callable[[SourceBatch, QueryCircle], Awaitable[None]]
WATCH_CIRCLE = QueryCircle("watch", 0.0, 0.0, 0.0, WATCH_INTERVAL_S, "watch")
FEED_CIRCLE = QueryCircle("feed", 0.0, 0.0, 0.0, 1.0, "feed")


def request_cost(src: Any, query: AreaQuery) -> float:
    return float(src.cost(query)) if isinstance(src, MeteredSource) else 1.0


def _contained(inner: QueryCircle, outer: QueryCircle) -> bool:
    d = geo.haversine_nm(inner.lat, inner.lon, outer.lat, outer.lon)
    return inner.key != outer.key and d + inner.radius_nm <= outer.radius_nm + 1e-6


@dataclass
class SourceSlot:
    source: AreaSource | FeedSource
    bucket: TokenBucket | None  # None: no rate limit (own receiver)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    requests: int = 0
    errors: int = 0
    day: int = 0
    requests_today: int = 0
    units_today: float = 0.0  # requests, or credits for metered sources
    last_latency_ms: float | None = None
    last_aircraft: int | None = None
    recent: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=2048))
    next_due: dict[str, float] = field(default_factory=dict)
    empty_streak: dict[str, int] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.source.name

    @property
    def is_feed(self) -> bool:
        return isinstance(self.source, FeedSource)

    def count(self, now: float, cost: float) -> None:
        self.requests += 1
        day = int(now // 86400)
        if day != self.day:
            self.day, self.requests_today, self.units_today = day, 0, 0.0
        self.requests_today += 1
        self.units_today += cost
        self.recent.append((now, cost))

    def used_per_s(self, now: float) -> float:
        while self.recent and now - self.recent[0][0] > RATE_WINDOW_S:
            self.recent.popleft()
        if not self.recent:
            return 0.0
        span = max(min(RATE_WINDOW_S, now - self.recent[0][0]), 60.0)
        return sum(c for _, c in self.recent) / span


async def _fetch_hexes(hexes: list[str], src: LookupSource) -> SourceBatch:
    return await src.fetch_hexes(hexes)


async def _fetch_registration(registration: str, src: LookupSource) -> SourceBatch:
    return await src.fetch_registration(registration)


class Poller:
    def __init__(
        self,
        sources: list[AreaSource | FeedSource],
        clock: Clock,
        circles: Callable[[], list[QueryCircle]],
        on_batch: BatchHandler,
        watch: Watchlist | None = None,
    ) -> None:
        self.slots = [self._slot(s) for s in sources]
        self.clock = clock
        self.circles = circles
        self.on_batch = on_batch
        self.last_circles: list[QueryCircle] = []
        self.watch = watch

    @staticmethod
    def _slot(src: AreaSource | FeedSource) -> SourceSlot:
        if isinstance(src, FeedSource):
            return SourceSlot(src, None)
        burst = max(1.0, src.max_cost) if isinstance(src, MeteredSource) else 1.0
        return SourceSlot(src, TokenBucket(src.max_rate_per_s * BUDGET_FRACTION, burst))

    def _interval(self, slot: SourceSlot, c: QueryCircle) -> float:
        if slot.empty_streak.get(c.key, 0) >= IDLE_AFTER_EMPTY_POLLS:
            return max(c.interval_s, IDLE_INTERVAL_S)
        return c.interval_s

    def circles_for(self, slot: SourceSlot, circles: list[QueryCircle]) -> list[QueryCircle]:
        """A source whose budget can't poll every circle at its interval skips circles that lie
        inside another one: it still covers the whole area, just less often near the places."""
        if slot.bucket is None:
            return []
        demand = sum(
            request_cost(slot.source, AreaQuery(c.lat, c.lon, c.radius_nm)) / c.interval_s
            for c in circles
        )
        if demand <= slot.bucket.rate:
            return circles
        return [c for c in circles if not any(_contained(c, o) for o in circles)]

    async def _request(
        self, slot: SourceSlot, call: Callable[[], Awaitable[SourceBatch]], cost: float
    ) -> SourceBatch | None:
        """One request: counts it, times it, and feeds the breaker. None on failure."""
        slot.count(self.clock.now(), cost)
        started = time.perf_counter()
        try:
            batch = await call()
        except SourceError as e:
            slot.errors += 1
            slot.breaker.failure(self.clock.now(), str(e), e.is_client_error, e.retry_after_s)
            log.warning("%s failed: %s", slot.name, e)
            return None
        slot.last_latency_ms = (time.perf_counter() - started) * 1000
        slot.breaker.success(self.clock.now())
        return batch

    async def _deliver(self, batch: SourceBatch, circle: QueryCircle) -> None:
        try:
            await self.on_batch(batch, circle)
        except Exception:
            log.exception("batch handler failed")

    async def poll_area(self, slot: SourceSlot, circles: list[QueryCircle]) -> float:
        """Poll this source's due circles. Returns the time it next has something to do."""
        assert slot.bucket is not None and not isinstance(slot.source, FeedSource)
        src = slot.source
        mine = self.circles_for(slot, circles)
        keys = {c.key for c in mine}
        for k in list(slot.next_due):
            if k not in keys:
                del slot.next_due[k]
        for c in sorted(mine, key=lambda c: slot.next_due.get(c.key, 0.0)):
            now = self.clock.now()
            if slot.next_due.get(c.key, 0.0) > now:
                continue
            if slot.breaker.is_open(now):
                break
            query = AreaQuery(c.lat, c.lon, c.radius_nm)
            cost = request_cost(src, query)
            if not slot.bucket.take(now, cost):
                break  # out of budget: the oldest-due circle goes first next time
            batch = await self._request(slot, partial(src.fetch, query), cost)
            now = self.clock.now()
            if batch is None:
                slot.next_due[c.key] = now + RETRY_S
                continue
            slot.last_aircraft = len(batch.observations)
            slot.empty_streak[c.key] = (
                0 if batch.observations else slot.empty_streak.get(c.key, 0) + 1
            )
            slot.next_due[c.key] = now + self._interval(slot, c)
            await self._deliver(batch, c)
        now = self.clock.now()
        if not mine:
            return now + 5.0
        wake = min(slot.next_due.get(c.key, now) for c in mine)
        cost = min(request_cost(src, AreaQuery(c.lat, c.lon, c.radius_nm)) for c in mine)
        return max(wake, slot.breaker.open_until, now + slot.bucket.wait_time(now, cost))

    async def poll_feed(self, slot: SourceSlot) -> float:
        assert isinstance(slot.source, FeedSource)
        src = slot.source
        now = self.clock.now()
        due = slot.next_due.get(FEED_CIRCLE.key, 0.0)
        if due > now or slot.breaker.is_open(now):
            return max(due, slot.breaker.open_until)
        batch = await self._request(slot, src.fetch_all, 1.0)
        slot.next_due[FEED_CIRCLE.key] = self.clock.now() + src.interval_s
        if batch is not None:
            slot.last_aircraft = len(batch.observations)
            await self._deliver(batch, FEED_CIRCLE)
        return max(slot.next_due[FEED_CIRCLE.key], slot.breaker.open_until)

    async def poll_slot(self, slot: SourceSlot) -> float:
        if slot.is_feed:
            return await self.poll_feed(slot)
        circles = self.circles()
        self.last_circles = circles
        return await self.poll_area(slot, circles)

    async def tick(self) -> float:
        """Poll everything that is due once. Returns seconds until something is due again."""
        wakes = [await self.poll_slot(slot) for slot in self.slots]
        await self.watch_tick()
        if self.watch is not None:
            wakes.append(self.watch.next_due)
        if not wakes:
            return 5.0
        return max(0.0, min(wakes) - self.clock.now())

    async def _call(
        self, call: Callable[[LookupSource], Awaitable[SourceBatch]]
    ) -> SourceBatch | None:
        """A lookup: the first healthy lookup source answers; each request spends its budget."""
        for slot in self.slots:
            src = slot.source
            if not isinstance(src, LookupSource) or slot.bucket is None:
                continue
            now = self.clock.now()
            if slot.breaker.is_open(now):
                continue
            if not slot.bucket.take(now):
                return None  # healthy but out of budget: wait, don't fail over
            batch = await self._request(slot, partial(call, src), 1.0)
            if batch is not None:
                return batch
        return None

    async def watch_tick(self) -> None:
        """Look up followed aircraft wherever they are: one request per 40 hexes each minute,
        plus an occasional lookup for a registration the database doesn't know."""
        w = self.watch
        if w is None:
            return
        batches: list[SourceBatch] = []
        now = self.clock.now()
        if now >= w.next_due:
            hexes = w.hexes()
            for i in range(0, len(hexes), MAX_HEXES_PER_REQUEST):
                chunk = hexes[i : i + MAX_HEXES_PER_REQUEST]
                batch = await self._call(partial(_fetch_hexes, chunk))
                if batch is None:
                    w.next_due = self.clock.now() + 5.0  # out of budget or all sources down
                    break
                batches.append(batch)
            else:
                w.next_due = now + WATCH_INTERVAL_S
                w.last_run = now
                w.last_found = sum(len(b.observations) for b in batches)
        reg = w.due_registration(self.clock.now())
        if reg is not None:
            batch = await self._call(partial(_fetch_registration, reg))
            if batch is not None:  # otherwise retried on a later tick
                w.looked_up(reg, self.clock.now())
                batches.append(batch)
        for batch in batches:
            for o in batch.observations:
                w.learn(o.hex, o.registration, batch.fetched_at)
            await self._deliver(batch, WATCH_CIRCLE)

    async def _loop(self, step: Callable[[], Awaitable[float]], name: str) -> None:
        while True:
            try:
                wait = await step() - self.clock.now()
            except Exception:
                log.exception("%s poll failed", name)
                wait = 5.0
            await asyncio.sleep(min(max(wait, 0.25), MAX_SLEEP_S))

    async def _watch_step(self) -> float:
        await self.watch_tick()
        return self.watch.next_due if self.watch is not None else self.clock.now() + 60.0

    async def run(self) -> None:
        """One loop per source, so a slow source never delays the others (or the receiver)."""
        loops = [self._loop(partial(self.poll_slot, s), s.name) for s in self.slots]
        loops.append(self._loop(self._watch_step, "watch"))
        await asyncio.gather(*loops)

    def status(self) -> list[dict[str, Any]]:
        now = self.clock.now()
        out = []
        for s in self.slots:
            src = s.source
            metered = isinstance(src, MeteredSource)
            budget = s.bucket.rate if s.bucket is not None else None
            extra = getattr(src, "status_extra", None)
            out.append(
                {
                    "name": s.name,
                    "kind": "receiver" if s.is_feed else "area",
                    "healthy": not s.breaker.is_open(now),
                    "open_for_s": max(0.0, s.breaker.open_until - now),
                    "last_ok_at": s.breaker.last_ok_at,
                    "last_error": s.breaker.last_error,
                    "requests": s.requests,
                    "requests_today": s.requests_today,
                    "errors": s.errors,
                    "budget_unit": "credit" if metered else "request",
                    "budget_per_s": budget,
                    "budget_per_day": budget * 86400 if budget is not None else None,
                    "used_per_s": s.used_per_s(now),
                    "used_today": s.units_today,
                    "interval_s": src.interval_s if isinstance(src, FeedSource) else None,
                    "last_latency_ms": s.last_latency_ms,
                    "last_aircraft": s.last_aircraft,
                    **(extra() if callable(extra) else {}),
                }
            )
        return out
