"""hexdb.io per-aircraft lookups: the last resort for registration, type and owner.

Only for aircraft we're tracking whose identity is still incomplete after the live data,
tar1090-db and the FAA registry, one request at a time (≥2 s apart, daily cap), filter
candidates first. Answers go into the SQLite `cache` table: hits for 30 days, "not found"
for 3. hexdb.io publishes no rate limit, so we stay far below anything that could matter
and back off hard on any error (CLAUDE.md etiquette).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

import httpx

from app.clock import Clock
from app.db import Database
from app.ingest.ratelimit import CircuitBreaker

log = logging.getLogger(__name__)

AIRCRAFT_URL = "https://hexdb.io/api/v2/aircraft/{hex}"
NAMESPACE = "hexdb"
MIN_INTERVAL_S = 2.0
DAILY_CAP = 2000
HIT_TTL_S = 30 * 86400.0
MISS_TTL_S = 3 * 86400.0
QUEUE_LIMIT = 500
FILTER_CANDIDATE, TRACKED = 0, 1  # queue priorities: lower goes first
_HEX = re.compile(r"^[0-9a-f]{6}$")
_PLACEHOLDERS = {"", "n/a", "na", "none", "null", "unknown", "-"}


def _text(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    v = " ".join(v.split())
    return None if v.lower() in _PLACEHOLDERS else v


def parse_aircraft(data: Any) -> dict[str, str | None] | None:
    """The v2 aircraft object → our fields. None when it carries nothing useful."""
    if not isinstance(data, dict):
        return None
    mfr, model = _text(data.get("manufacturer")), _text(data.get("type"))
    type_code = _text(data.get("icao_type_code"))
    out = {
        "registration": _text(data.get("registration")),
        "type_code": type_code.upper() if type_code else None,
        "type_desc": " ".join(p for p in (mfr, model) if p) or None,
        "owner_op": _text(data.get("registered_owner")),
    }
    return out if any(out.values()) else None


def cache_fresh(value: str | None, fetched_at: float, now: float) -> bool:
    return now - fetched_at < (HIT_TTL_S if value else MISS_TTL_S)


class HexdbService:
    def __init__(
        self,
        db: Database,
        http: httpx.AsyncClient,
        clock: Clock,
        enabled: bool = True,
        daily_cap: int = DAILY_CAP,
    ) -> None:
        self.db = db
        self.http = http
        self.clock = clock
        self.enabled = enabled
        self.daily_cap = daily_cap
        self.pending: dict[str, int] = {}  # hex → priority
        self.breaker = CircuitBreaker()
        self.last_request_at = float("-inf")
        self.day = -1
        self.requests_today = 0
        self.requests = 0
        self.found = 0
        self.not_found = 0

    @property
    def last_error(self) -> str | None:
        return self.breaker.last_error

    def want(self, hex_: str, priority: int = TRACKED) -> None:
        if not self.enabled or not _HEX.match(hex_):
            return
        current = self.pending.get(hex_)
        if current is not None:
            self.pending[hex_] = min(current, priority)
        elif len(self.pending) < QUEUE_LIMIT:
            self.pending[hex_] = priority
        elif priority == FILTER_CANDIDATE:
            oldest = next((h for h, p in self.pending.items() if p != FILTER_CANDIDATE), None)
            if oldest is not None:
                del self.pending[oldest]
                self.pending[hex_] = priority

    def prioritize(self, hex_: str) -> None:
        if hex_ in self.pending:
            self.pending[hex_] = FILTER_CANDIDATE

    def _budget_left(self, now: float) -> bool:
        day = int(now // 86400)
        if day != self.day:
            self.day, self.requests_today = day, 0
        return self.requests_today < self.daily_cap

    def due(self, now: float) -> bool:
        return (
            self.enabled
            and bool(self.pending)
            and now - self.last_request_at >= MIN_INTERVAL_S
            and not self.breaker.is_open(now)
            and self._budget_left(now)
        )

    async def flush(self, relevant: Callable[[str], bool]) -> str | None:
        """Look up one queued aircraft. Returns its hex when a new answer was cached."""
        now = self.clock.now()
        for h in [h for h in self.pending if not relevant(h)]:
            del self.pending[h]  # gone out of range: not worth a request
        hex_ = None
        while self.due(now):
            candidate = min(self.pending, key=lambda h: self.pending[h])  # oldest first on ties
            del self.pending[candidate]
            row = await self.db.fetchone(
                "SELECT value, fetched_at FROM cache WHERE namespace = ? AND key = ?",
                (NAMESPACE, candidate),
            )
            if row is None or not cache_fresh(row["value"], row["fetched_at"], now):
                hex_ = candidate
                break
        if hex_ is None:
            return None
        self.last_request_at = now
        self.requests_today += 1
        self.requests += 1
        try:
            resp = await self.http.get(AIRCRAFT_URL.format(hex=hex_), timeout=10)
        except httpx.HTTPError as e:
            self.breaker.failure(now, f"hexdb.io: {type(e).__name__}", client_error=False)
            return None
        value: dict[str, str | None] | None
        if resp.status_code == 200:
            try:
                value = parse_aircraft(resp.json())
            except ValueError:
                self.breaker.failure(now, "hexdb.io: invalid JSON", client_error=False)
                return None
        elif resp.status_code == 404 and _not_found(resp):
            value = None
        else:
            retry = _retry_after(resp)
            # Any other 4xx/5xx: back off hard (30 s doubling to 5 min, or Retry-After).
            self.breaker.failure(now, f"hexdb.io: HTTP {resp.status_code}", True, retry)
            log.warning("hexdb.io lookup failed: HTTP %s", resp.status_code)
            return None
        self.breaker.success(now)
        if value:
            self.found += 1
        else:
            self.not_found += 1
        await self.db.execute(
            "INSERT OR REPLACE INTO cache(namespace, key, value, fetched_at) VALUES (?, ?, ?, ?)",
            (NAMESPACE, hex_, json.dumps(value) if value else None, now),
        )
        return hex_

    def status(self) -> dict[str, Any]:
        now = self.clock.now()
        self._budget_left(now)
        return {
            "enabled": self.enabled,
            "pending": len(self.pending),
            "requests": self.requests,
            "requests_today": self.requests_today,
            "daily_cap": self.daily_cap,
            "found": self.found,
            "not_found": self.not_found,
            "backing_off": self.breaker.is_open(now),
            "last_error": self.breaker.last_error,
        }


def _not_found(resp: httpx.Response) -> bool:
    """hexdb.io's documented "no such aircraft" answer, as opposed to a wrong path."""
    try:
        err = resp.json().get("error")
    except (ValueError, AttributeError):
        return False
    return isinstance(err, dict) and err.get("code") == "aircraft_not_found"


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers.get("retry-after", ""))
    except ValueError:
        return None
