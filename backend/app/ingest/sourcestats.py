"""Sources report: per source, how many aircraft it saw in your polled areas, and how many of
those no other source saw (its unique contribution). That is how you judge whether a source,
especially a paid one, is worth keeping.

Memory is bounded: 15-minute buckets of hex → bitmask of sources for the last 24 h (capped per
bucket), and per-day totals for the last 14 days (persisted by the runtime).
"""

from __future__ import annotations

from typing import Any

BUCKET_S = 900.0
WINDOW_S = 86400.0
MAX_HEXES_PER_BUCKET = 20_000
MAX_SOURCES = 30
KEEP_DAYS = 14
WINDOWS = (("last_hour", 3600.0), ("last_24h", WINDOW_S))


def _count(masks: dict[str, int], bits: dict[str, int]) -> dict[str, list[int]]:
    """source → [aircraft seen, aircraft only it saw]."""
    out = {name: [0, 0] for name in bits}
    for mask in masks.values():
        for name, bit in bits.items():
            if mask & bit:
                out[name][0] += 1
                if mask == bit:
                    out[name][1] += 1
    return out


class SourceStats:
    def __init__(self) -> None:
        self.bits: dict[str, int] = {}
        self.buckets: dict[int, dict[str, int]] = {}  # bucket start → hex → source bitmask
        # epoch day (UTC) → {"aircraft": n, "sources": {name: [seen, unique]}}
        self.days: dict[int, dict[str, Any]] = {}
        self.day: int | None = None
        self.dropped = 0
        self.dirty = False

    def load(self, data: dict[str, Any]) -> None:
        self.days = {int(d): v for d, v in data.items()}

    def to_json(self) -> dict[str, Any]:
        return {str(d): v for d, v in self.days.items()}

    def _bit(self, source: str) -> int | None:
        bit = self.bits.get(source)
        if bit is None and len(self.bits) < MAX_SOURCES:
            bit = self.bits[source] = 1 << len(self.bits)
        return bit

    def add(self, source: str, hex_: str, now: float) -> None:
        self._roll(now)
        bit = self._bit(source)
        if bit is None:
            return
        start = int(now // BUCKET_S * BUCKET_S)
        bucket = self.buckets.setdefault(start, {})
        mask = bucket.get(hex_)
        if mask is None and len(bucket) >= MAX_HEXES_PER_BUCKET:
            self.dropped += 1
            return
        bucket[hex_] = (mask or 0) | bit

    def _roll(self, now: float) -> None:
        day = int(now // 86400)
        if self.day is not None and day != self.day:
            self.days[self.day] = self._summary(self.day * 86400.0, (self.day + 1) * 86400.0)
            for d in sorted(self.days)[:-KEEP_DAYS]:
                del self.days[d]
            self.dirty = True
        self.day = day
        cutoff = now - WINDOW_S - BUCKET_S
        for start in [s for s in self.buckets if s < cutoff]:
            del self.buckets[start]

    def _union(self, start: float, end: float) -> dict[str, int]:
        out: dict[str, int] = {}
        for s, bucket in self.buckets.items():
            if start <= s < end:
                for h, m in bucket.items():
                    out[h] = out.get(h, 0) | m
        return out

    def _summary(self, start: float, end: float) -> dict[str, Any]:
        masks = self._union(start, end)
        return {"aircraft": len(masks), "sources": _count(masks, self.bits)}

    def report(self, now: float, names: list[str]) -> dict[str, Any]:
        """Windows end now; "last hour" starts at a bucket boundary, so it spans 60–75 min."""
        self._roll(now)
        windows = {}
        for key, span in WINDOWS:
            start = (now - span) // BUCKET_S * BUCKET_S
            windows[key] = self._summary(start, now + 1)
        days = []
        today = int(now // 86400)
        for d in sorted(self.days, reverse=True):
            if d != today:
                days.append({"day": d * 86400, **self.days[d]})
        known = list(dict.fromkeys([*names, *self.bits]))
        return {
            "sources": known,
            "windows": windows,
            "today": {"day": today * 86400, **self._summary(today * 86400.0, now + 1)},
            "days": days,
            "dropped": self.dropped,
        }
