"""Flightradar24 as a paid, never-polled source: on-demand lookups and gap-fill for filter
candidates the free feeds lost.

Every request passes, in order: the on/off switch, the per-identifier cache (`cache` table,
namespace `fr24`), the monthly credit cap (`CreditMeter`; gap-fill also stays under the even-pace
line), the circuit breaker, and a token bucket at half of FR24's published rate. Credits are
charged per flight returned, at least one per request.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from app.clock import Clock
from app.config import Settings
from app.db import Database
from app.sources.base import SourceBatch, SourceError
from app.sources.fr24 import (
    MAX_PER_PARAM,
    FR24Flight,
    FR24Source,
    norm_reg,
    to_batch,
    valid_callsign,
    valid_registration,
)

from .credits import CreditMeter
from .ratelimit import CircuitBreaker, TokenBucket

log = logging.getLogger(__name__)

Purpose = Literal["on_demand", "gap_fill"]
BUDGET_FRACTION = 0.5
SETTINGS_KEY = "fr24_credits"  # not a backed-up setting
CACHE_NS = "fr24"
FOUND_TTL_S = 60.0
MISSING_TTL_S = 240.0
CACHE_KEEP_S = 86400.0
USAGE_TTL_S = 600.0
# Gap-fill: an aircraft counts as lost when no free source has heard it for this long.
LOST_AFTER_S = 90.0
GAP_FILL_TICK_S = 30.0
GAP_FILL_INTERVAL_S = 300.0  # per aircraft, while FR24 keeps finding it
GAP_FILL_MAX_BACKOFF_S = 6 * 3600.0  # per aircraft, while FR24 doesn't find it either
MAX_GAP_TARGETS = 200


class LookupRefusedError(Exception):
    """The lookup wasn't sent (switched off, cap, pace, rate or breaker) or failed."""

    def __init__(self, reason: str, retry_in_s: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_in_s = retry_in_s


@dataclass
class LookupResult:
    flights: list[FR24Flight]
    batch: SourceBatch
    from_cache: bool
    credits: float


def _keys(registrations: list[str], callsigns: list[str]) -> dict[str, str]:
    """Cache key → the identifier as sent to FR24 (registrations keep their hyphens)."""
    keys = {f"reg:{norm_reg(r)}": r.strip().upper() for r in registrations if valid_registration(r)}
    for c in callsigns:
        if valid_callsign(c):
            keys[f"cs:{c.strip().upper()}"] = c.strip().upper()
    return keys


def _flight_keys(f: FR24Flight) -> set[str]:
    out = set()
    if f.registration:
        out.add(f"reg:{norm_reg(f.registration)}")
    if f.callsign:
        out.add(f"cs:{f.callsign}")
    return out


class FR24Service:
    name = "fr24"

    def __init__(
        self,
        source: FR24Source,
        db: Database,
        clock: Clock,
        monthly_credits: float,
        credits_per_flight: float,
        gap_fill: bool = True,
    ) -> None:
        self.source = source
        self.db = db
        self.clock = clock
        self.credits_per_flight = credits_per_flight
        self.gap_fill_enabled = gap_fill
        self.bucket = TokenBucket(source.max_rate_per_s * BUDGET_FRACTION, 1.0)
        self.breaker = CircuitBreaker()
        self.meter = CreditMeter(monthly_credits)
        self.enabled = True
        self.requests = 0
        self.errors = 0
        self.day = 0
        self.requests_today = 0
        self.credits_today = 0.0
        self.counts = {"on_demand": 0, "gap_fill": 0, "cache_hits": 0, "found": 0}
        self.last_latency_ms: float | None = None
        self.last_aircraft: int | None = None
        self._usage: tuple[float, list[dict[str, Any]]] | None = None

    # ---- persistence -------------------------------------------------------------------------

    async def load(self) -> None:
        self.meter.load(await self.db.get_json(SETTINGS_KEY, {}))

    async def save(self) -> None:
        if self.meter.dirty:
            self.meter.dirty = False
            await self.db.set_json(SETTINGS_KEY, self.meter.to_json())

    async def _cached(self, keys: list[str], now: float) -> dict[str, list[FR24Flight]]:
        if not keys:
            return {}
        marks = ",".join("?" * len(keys))
        rows = await self.db.fetchall(
            f"SELECT key, value, fetched_at FROM cache WHERE namespace = ? AND key IN ({marks})",  # noqa: S608
            (CACHE_NS, *keys),
        )
        out: dict[str, list[FR24Flight]] = {}
        for r in rows:
            try:
                flights = [FR24Flight.from_dict(d) for d in json.loads(r["value"] or "[]")]
            except (ValueError, TypeError):
                continue
            ttl = FOUND_TTL_S if flights else MISSING_TTL_S
            if now - r["fetched_at"] < ttl:
                out[r["key"]] = flights
        return out

    async def _store(self, keys: list[str], flights: list[FR24Flight], now: float) -> None:
        rows = []
        for k in keys:
            mine = [f.to_dict() for f in flights if k in _flight_keys(f)]
            rows.append((CACHE_NS, k, json.dumps(mine), now))
        await self.db.executemany(
            "INSERT OR REPLACE INTO cache(namespace, key, value, fetched_at) VALUES (?, ?, ?, ?)",
            rows,
        )
        await self.db.execute(
            "DELETE FROM cache WHERE namespace = ? AND fetched_at < ?",
            (CACHE_NS, now - CACHE_KEEP_S),
        )

    # ---- lookups -----------------------------------------------------------------------------

    def _count(self, now: float, credits: float) -> None:
        self.requests += 1
        day = int(now // 86400)
        if day != self.day:
            self.day, self.requests_today, self.credits_today = day, 0, 0.0
        self.requests_today += 1
        self.credits_today += credits

    def _gate(self, now: float, cost: float, purpose: Purpose) -> None:
        if not self.enabled:
            raise LookupRefusedError("Flightradar24 is switched off in the Sources report")
        if purpose == "gap_fill" and not self.gap_fill_enabled:
            raise LookupRefusedError("gap-fill is off (FR24_GAP_FILL)")
        reason = self.meter.refusal(now, cost, paced=purpose == "gap_fill")
        if reason is not None:
            raise LookupRefusedError(reason)
        if self.breaker.is_open(now):
            wait = self.breaker.open_until - now
            reason = f"backing off after an error ({self.breaker.last_error})"
            raise LookupRefusedError(reason, wait)
        wait = self.bucket.wait_time(now)
        if wait > 0:
            raise LookupRefusedError("rate limit: one request every few seconds", wait)

    async def lookup(
        self, registrations: list[str], callsigns: list[str], purpose: Purpose
    ) -> LookupResult:
        """Live positions for these registrations/callsigns, from the cache when fresh."""
        now = self.clock.now()
        keys = _keys(registrations, callsigns)
        if not keys:
            raise LookupRefusedError("nothing to look up: no valid registration or callsign")
        cached = await self._cached(list(keys), now)
        missing = [k for k in keys if k not in cached]
        flights: dict[str, FR24Flight] = {}
        for fl in cached.values():
            for f in fl:
                flights[f.fr24_id] = f
        credits = 0.0
        if not missing:
            self.counts["cache_hits"] += 1
        else:
            sent = [k for k in missing if k.startswith("reg:")][:MAX_PER_PARAM]
            sent += [k for k in missing if k.startswith("cs:")][:MAX_PER_PARAM]
            regs = [keys[k] for k in sent if k.startswith("reg:")]
            css = [keys[k] for k in sent if k.startswith("cs:")]
            # `limit` = identifiers sent, so this is the most a request can cost.
            worst = self.credits_per_flight * len(sent)
            self._gate(now, worst, purpose)
            self.bucket.take(now)
            self.meter.reserve(now, worst)
            started = time.perf_counter()
            try:
                found = await self.source.fetch_flights(regs, css)
            except SourceError as e:
                self.errors += 1
                self._count(now, 0.0)
                self.meter.settle(self.clock.now(), worst, 0.0)
                if e.status == 402:  # Payment Required: the plan's credits are gone
                    self.meter.mark_exhausted(now)
                self.breaker.failure(now, str(e), e.is_client_error, e.retry_after_s)
                await self.save()
                log.warning("fr24 lookup failed: %s", e)
                raise LookupRefusedError(f"Flightradar24: {e}") from e
            now = self.clock.now()
            self.last_latency_ms = (time.perf_counter() - started) * 1000
            self.breaker.success(now)
            credits = self.credits_per_flight * max(1, len(found))
            self.meter.settle(now, worst, credits)
            self._count(now, credits)
            self.counts[purpose] += 1
            self.counts["found"] += len(found)
            self.last_aircraft = len(found)
            await self.save()
            await self._store(sent, found, now)
            for f in found:
                flights[f.fr24_id] = f
        out = list(flights.values())
        return LookupResult(out, to_batch(out, now), not missing, credits)

    async def usage(self) -> list[dict[str, Any]]:
        """FR24's own credit count over the last 30 days, to calibrate `credits_per_flight`."""
        now = self.clock.now()
        if self._usage is not None and now - self._usage[0] < USAGE_TTL_S:
            return self._usage[1]
        if not self.enabled:
            raise LookupRefusedError("Flightradar24 is switched off in the Sources report")
        if self.breaker.is_open(now):
            raise LookupRefusedError("backing off after an error", self.breaker.open_until - now)
        if not self.bucket.take(now):
            raise LookupRefusedError("rate limit", self.bucket.wait_time(now))
        try:
            rows = await self.source.fetch_usage("30d")
        except SourceError as e:
            self.errors += 1
            self.breaker.failure(now, str(e), e.is_client_error, e.retry_after_s)
            raise LookupRefusedError(f"Flightradar24: {e}") from e
        self.breaker.success(self.clock.now())
        self._usage = (now, rows)
        return rows

    # ---- report ------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Same shape as the poller's per-source rows, plus the month's credit meter."""
        now = self.clock.now()
        if int(now // 86400) != self.day:
            self.day, self.requests_today, self.credits_today = int(now // 86400), 0, 0.0
        return {
            "name": self.name,
            "kind": "lookup",
            "enabled": self.enabled,
            "healthy": not self.breaker.is_open(now),
            "open_for_s": max(0.0, self.breaker.open_until - now),
            "last_ok_at": self.breaker.last_ok_at,
            "last_error": self.breaker.last_error,
            "requests": self.requests,
            "requests_today": self.requests_today,
            "errors": self.errors,
            "budget_unit": "credit",
            "budget_per_s": None,
            "budget_per_day": None,
            "used_per_s": 0.0,
            "used_today": self.credits_today,
            "interval_s": None,
            "last_latency_ms": self.last_latency_ms,
            "last_aircraft": self.last_aircraft,
            "month": self.meter.status(now),
            "credits_per_flight": self.credits_per_flight,
            "gap_fill": self.gap_fill_enabled,
            "lookups": dict(self.counts),
        }


# ---- gap-fill planning ---------------------------------------------------------------------


@dataclass
class GapTarget:
    key: str  # "watch:reg:N123AB", "watch:hex:ae1234" or "hex:ae1234"
    registration: str | None = None
    callsign: str | None = None


@dataclass
class GapFiller:
    """Per-aircraft schedule for gap-fill: every 5 min while FR24 finds it, backing off up to
    6 h while it doesn't; forgotten as soon as a free source hears it again."""

    state: dict[str, tuple[float, float]] = field(default_factory=dict)  # key → (due, backoff)

    def due(self, targets: list[GapTarget], now: float) -> list[GapTarget]:
        keys = {t.key for t in targets}
        for k in [k for k in self.state if k not in keys]:
            del self.state[k]  # found again by the free feeds, or no longer wanted
        due = [t for t in targets if self.state.get(t.key, (0.0, 0.0))[0] <= now]
        due.sort(key=lambda t: self.state.get(t.key, (0.0, 0.0))[0])
        regs = css = 0
        out = []
        seen: set[str] = set()
        for t in due:
            if t.registration and valid_registration(t.registration):
                ident = f"reg:{norm_reg(t.registration)}"
                if regs >= MAX_PER_PARAM:
                    continue
            elif t.callsign and valid_callsign(t.callsign):
                ident = f"cs:{t.callsign.upper()}"
                if css >= MAX_PER_PARAM:
                    continue
            else:
                continue
            if ident in seen:
                continue
            seen.add(ident)
            if ident.startswith("reg:"):
                regs += 1
            else:
                css += 1
            out.append(t)
        return out

    def done(self, target: GapTarget, found: bool, now: float) -> None:
        if found:
            self.state[target.key] = (now + GAP_FILL_INTERVAL_S, 0.0)
            return
        _, backoff = self.state.get(target.key, (0.0, 0.0))
        backoff = min(GAP_FILL_MAX_BACKOFF_S, max(GAP_FILL_INTERVAL_S * 2, backoff * 2))
        self.state[target.key] = (now + backoff, backoff)

    def wait(self, target: GapTarget, now: float, seconds: float) -> None:
        """Not sent (cap, pace, rate): try again later without counting it as a miss."""
        _, backoff = self.state.get(target.key, (0.0, 0.0))
        self.state[target.key] = (now + seconds, backoff)


def matched_by(flights: list[FR24Flight], t: GapTarget) -> bool:
    for f in flights:
        if (
            t.registration
            and f.registration
            and norm_reg(f.registration) == norm_reg(t.registration)
        ):
            return True
        if t.callsign and f.callsign == t.callsign.upper():
            return True
    return False


def build_fr24(
    settings: Settings, http: httpx.AsyncClient, clock: Clock, db: Database
) -> FR24Service | None:
    if not settings.fr24_api_key:
        return None
    return FR24Service(
        FR24Source(http, clock, settings.fr24_api_key), db, clock,
        settings.fr24_monthly_credits, settings.fr24_credits_per_flight, settings.fr24_gap_fill,
    )  # fmt: skip
