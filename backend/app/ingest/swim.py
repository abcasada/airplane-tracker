"""FAA SWIM feed runtime: the Solace connection as an asyncio task, with exponential backoff.

The Solace client is threaded and blocking. Its dispatch thread hands each payload to `offer`,
which inflates it, drops documents that mention none of your airports or followed aircraft (a
byte regex on the message headers: the national TFMS stream is large) and appends the rest to a
bounded queue, dropping the oldest when full. An asyncio task drains the queue, parses in a
worker thread and merges the flights into `SwimFlights`. Nothing here blocks the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.clock import Clock
from app.config import Settings
from app.sources.swim import (
    NAME,
    FlightUpdate,
    ParseResult,
    SolaceConnector,
    SwimConfig,
    Wanted,
    airport_aliases,
    decode_payload,
    interest_pattern,
    parse_payload,
)

from .swimstore import FiledFlight, SwimFlights

log = logging.getLogger(__name__)

QUEUE_MAX = 500  # documents
QUEUE_MAX_BYTES = 8_000_000
PARSE_CHUNK = 50
BACKOFF_MIN_S = 10.0
BACKOFF_MAX_S = 600.0
CHECK_INTERVAL_S = 5.0
RATE_WINDOW_MIN = 5


class Connector(Protocol):
    def connect(self) -> None: ...

    def is_connected(self) -> bool: ...

    def close(self) -> None: ...


ConnectorFactory = Callable[[SwimConfig, Callable[[bytes], None]], Connector]
FlightsHandler = Callable[[list[FiledFlight]], Awaitable[None]]


def _parse_all(docs: list[bytes], wanted: Wanted) -> ParseResult:
    out = ParseResult([])
    for d in docs:
        r = parse_payload(d, wanted)
        out.updates += r.updates
        out.messages += r.messages
        out.skipped += r.skipped
        out.errors += r.errors
    return out


class SwimFeed:
    def __init__(
        self,
        cfg: SwimConfig,
        clock: Clock,
        store: SwimFlights,
        on_flights: FlightsHandler,
        connector_factory: ConnectorFactory = SolaceConnector,
    ) -> None:
        self.name = NAME
        self.cfg = cfg
        self.clock = clock
        self.store = store
        self.on_flights = on_flights
        self.connector = connector_factory(cfg, self.offer)
        available = getattr(self.connector, "available", None)
        self.library_installed = bool(available()) if callable(available) else True
        self.enabled = True
        self.state = "starting"
        self.last_error: str | None = None
        self.last_ok_at: float | None = None
        self.connected_since: float | None = None
        self.retry_at: float | None = None
        self.errors = 0
        # Counters (written by the Solace thread under the lock).
        self.received = 0
        self.kept = 0  # documents about your airports/aircraft
        self.irrelevant = 0
        self.dropped = 0  # queue full (oldest dropped), unreadable or oversized
        self.parse_errors = 0
        self.flight_updates = 0
        self.last_message_at: float | None = None
        self._minutes: deque[list[int]] = deque(maxlen=RATE_WINDOW_MIN + 1)
        self._lock = threading.Lock()
        self._queue: deque[bytes] = deque()
        self._queued_bytes = 0
        self._wake_pending = False
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._interest: tuple[re.Pattern[bytes] | None, Wanted] = (None, _nothing)
        self.interest_size = (0, 0)

    # ---- what to keep ------------------------------------------------------------------------

    def set_interest(self, airports: set[str], acids: set[str]) -> None:
        codes = frozenset(a for c in airports for a in airport_aliases(c))
        ids = frozenset(acids)

        def wanted(dep: str | None, arr: str | None, acid: str) -> bool:
            return dep in codes or arr in codes or acid in ids

        # One tuple assignment: the Solace thread always sees a matching pair.
        self._interest = (interest_pattern(set(codes), set(ids)), wanted)
        self.interest_size = (len(airports), len(ids))

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    # ---- Solace dispatch thread --------------------------------------------------------------

    def offer(self, payload: bytes) -> None:
        """Called on Solace's thread for every message. Must stay quick and never raise."""
        now = self.clock.now()
        data = decode_payload(payload)
        pattern = self._interest[0]
        keep = data is not None and pattern is not None and pattern.search(data) is not None
        wake = False
        with self._lock:
            self.received += 1
            self.last_message_at = now
            minute = int(now // 60)
            if self._minutes and self._minutes[-1][0] == minute:
                self._minutes[-1][1] += 1
            else:
                self._minutes.append([minute, 1])
            if data is None:
                self.dropped += 1
            elif not keep:
                self.irrelevant += 1
            else:
                self.kept += 1
                self._queue.append(data)
                self._queued_bytes += len(data)
                while len(self._queue) > QUEUE_MAX or self._queued_bytes > QUEUE_MAX_BYTES:
                    self._queued_bytes -= len(self._queue.popleft())
                    self.dropped += 1
                wake = not self._wake_pending
                self._wake_pending = True
        if wake and self._loop is not None and self._wake is not None:
            with contextlib.suppress(RuntimeError):  # loop closed during shutdown
                self._loop.call_soon_threadsafe(self._wake.set)

    def _take(self) -> list[bytes]:
        with self._lock:
            self._wake_pending = False
            docs = list(self._queue)
            self._queue.clear()
            self._queued_bytes = 0
        return docs

    # ---- asyncio side ------------------------------------------------------------------------

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        try:
            await asyncio.gather(self._connection_loop(), self._consume_loop())
        finally:
            self.connector.close()

    async def _connection_loop(self) -> None:
        backoff = BACKOFF_MIN_S
        while True:
            if not self.enabled:
                if self.state != "off":
                    await asyncio.to_thread(self.connector.close)
                    self.state, self.connected_since = "off", None
                await asyncio.sleep(CHECK_INTERVAL_S)
                continue
            try:
                self.state = "connecting"
                await asyncio.to_thread(self.connector.connect)
                self.state, self.last_error, self.retry_at = "connected", None, None
                self.connected_since = self.last_ok_at = self.clock.now()
                backoff = BACKOFF_MIN_S
                while self.enabled and self.connector.is_connected():
                    self.last_ok_at = self.clock.now()
                    await asyncio.sleep(CHECK_INTERVAL_S)
                if not self.enabled:
                    continue
                raise ConnectionError("disconnected")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
                log.warning("SWIM connection failed: %s; retrying in %.0f s", e, backoff)
                await asyncio.to_thread(self.connector.close)
                self.state, self.connected_since = "backing off", None
                self.retry_at = self.clock.now() + backoff
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)

    async def _consume_loop(self) -> None:
        assert self._wake is not None
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self.drain()
            except Exception:
                log.exception("SWIM batch failed")

    async def drain(self) -> int:
        """Parse and store everything queued. Returns the number of flights that changed."""
        docs = self._take()
        changed = 0
        for i in range(0, len(docs), PARSE_CHUNK):
            r = await asyncio.to_thread(_parse_all, docs[i : i + PARSE_CHUNK], self._interest[1])
            self.parse_errors += r.errors
            changed += await self.ingest(r.updates)
        return changed

    async def ingest(self, updates: list[FlightUpdate]) -> int:
        self.flight_updates += len(updates)
        flights = await self.store.apply(updates)
        if flights:
            await self.on_flights(flights)
        return len(flights)

    # ---- status ------------------------------------------------------------------------------

    def messages_per_min(self, now: float) -> float:
        minute = int(now // 60)
        with self._lock:
            recent = [n for m, n in self._minutes if minute - RATE_WINDOW_MIN <= m < minute]
        span = RATE_WINDOW_MIN
        if self.connected_since is not None:
            span = max(1, min(RATE_WINDOW_MIN, minute - int(self.connected_since // 60)))
        return sum(recent) / span

    def status(self) -> dict[str, Any]:
        now = self.clock.now()
        with self._lock:
            queued = len(self._queue)
        connected = self.state == "connected"
        return {
            "name": self.name,
            "kind": "schedule",
            "enabled": self.enabled,
            "healthy": connected or self.state in ("starting", "connecting", "off"),
            "open_for_s": max(0.0, self.retry_at - now) if self.retry_at else 0.0,
            "last_ok_at": self.last_ok_at,
            "last_error": self.last_error,
            "requests": self.received,
            "requests_today": 0,
            "errors": self.errors,
            "budget_unit": "message",
            "budget_per_s": None,
            "budget_per_day": None,
            "used_per_s": 0.0,
            "used_today": 0.0,
            "interval_s": None,
            "last_latency_ms": None,
            "last_aircraft": None,
            "swim": {
                "state": self.state,
                "library_installed": self.library_installed,
                "connected_since": self.connected_since,
                "messages_per_min": round(self.messages_per_min(now), 1),
                "received": self.received,
                "kept": self.kept,
                "irrelevant": self.irrelevant,
                "dropped": self.dropped,
                "queued": queued,
                "parse_errors": self.parse_errors,
                "flight_updates": self.flight_updates,
                "flights": len(self.store.flights),
                "last_message_at": self.last_message_at,
                "airports": self.interest_size[0],
                "aircraft": self.interest_size[1],
            },
        }


def swim_config(settings: Settings) -> SwimConfig | None:
    """The SCDS connection, when every required setting is present."""
    s = settings
    if not (s.swim_host and s.swim_vpn and s.swim_username and s.swim_password and s.swim_queue):
        return None
    return SwimConfig(
        s.swim_host, s.swim_vpn, s.swim_username, s.swim_password, s.swim_queue,
        s.swim_trust_store,
    )  # fmt: skip


def _nothing(_dep: str | None, _arr: str | None, _acid: str) -> bool:
    return False
