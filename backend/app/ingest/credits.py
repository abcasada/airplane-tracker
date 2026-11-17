"""Monthly credit meter for paid sources: credits spent per calendar month (UTC), a hard cap, and
an even-pace line that background use (gap-fill) may not cross, so it can't use up the month
before the user's own lookups.

Pure state with `load`/`to_json`; the owner persists it in the `settings` table (never in
backups: it isn't one of the backed-up settings).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def month_key(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m")


def month_bounds(now: float) -> tuple[float, float]:
    d = datetime.fromtimestamp(now, UTC)
    start = datetime(d.year, d.month, 1, tzinfo=UTC)
    end = datetime(d.year + (d.month == 12), d.month % 12 + 1, 1, tzinfo=UTC)
    return start.timestamp(), end.timestamp()


@dataclass
class CreditMeter:
    cap: float
    month: str = ""
    used: float = 0.0
    requests: int = 0
    exhausted: bool = False  # the source said this month's credits are gone
    dirty: bool = False

    def _roll(self, now: float) -> None:
        m = month_key(now)
        if m != self.month:
            self.month, self.used, self.requests, self.exhausted = m, 0.0, 0, False
            self.dirty = True

    def pace_limit(self, now: float) -> float:
        """Credits background use may have spent by now: the cap spread evenly over the month,
        with one day's share up front so the first lookups of the month aren't blocked."""
        start, end = month_bounds(now)
        span = end - start
        return min(self.cap, self.cap * (now - start + 86400.0) / span)

    def refusal(self, now: float, cost: float, paced: bool) -> str | None:
        """Why a request costing `cost` credits may not be made now, or None if it may."""
        self._roll(now)
        if self.exhausted:
            return "the source reports this month's credits are used up"
        if self.used + cost > self.cap:
            return f"monthly cap reached ({self.used:,.0f} of {self.cap:,.0f} credits)"
        if paced and self.used + cost > self.pace_limit(now):
            return "ahead of the month's even pace; background lookups wait"
        return None

    def spend(self, now: float, cost: float) -> None:
        self._roll(now)
        self.used += cost
        self.requests += 1
        self.dirty = True

    def mark_exhausted(self, now: float) -> None:
        self._roll(now)
        self.exhausted = True
        self.dirty = True

    def status(self, now: float) -> dict[str, Any]:
        self._roll(now)
        _, end = month_bounds(now)
        return {
            "month": self.month,
            "used": round(self.used, 1),
            "cap": self.cap,
            "requests": self.requests,
            "pace_limit": round(self.pace_limit(now), 1),
            "exhausted": self.exhausted,
            "resets_at": end,
        }

    def load(self, data: dict[str, Any]) -> None:
        try:
            self.month = str(data.get("month", ""))
            self.used = float(data.get("used", 0.0))
            self.requests = int(data.get("requests", 0))
            self.exhausted = bool(data.get("exhausted", False))
        except (TypeError, ValueError):
            self.month, self.used, self.requests, self.exhausted = "", 0.0, 0, False

    def to_json(self) -> dict[str, Any]:
        return {
            "month": self.month, "used": self.used, "requests": self.requests,
            "exhausted": self.exhausted,
        }  # fmt: skip
