"""Records normalized observations for replay testing and backtests.

Layout: <data>/recordings/YYYY-MM-DD/HH.ndjson.gz (UTC). Each batch is appended as its own
gzip member, which gzip readers handle transparently.
"""

import gzip
import json
import logging
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.sources.base import Observation, SourceBatch

log = logging.getLogger(__name__)

# Short keys keep files small; replay maps them back onto Observation fields.
FIELDS = {
    "h": "hex", "s": "source", "st": "seen_at", "t": "pos_time", "la": "lat", "lo": "lon",
    "ab": "alt_baro_ft", "ag": "alt_geom_ft", "g": "on_ground", "gs": "gs_kt", "tr": "track_deg",
    "br": "baro_rate_fpm", "gr": "geom_rate_fpm", "cs": "callsign", "sq": "squawk",
    "em": "emergency", "ca": "category", "nh": "nav_heading_deg", "na": "nav_altitude_ft",
    "ps": "pos_source", "np": "nac_p", "r": "registration", "ty": "type_code", "de": "type_desc",
    "ow": "owner_op", "yr": "year", "df": "db_flags",
}  # fmt: skip
_DEFAULTS = {"on_ground": False, "pos_source": "other", "db_flags": 0}


def encode(obs: Observation) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for short, name in FIELDS.items():
        v = getattr(obs, name)
        if v is None or _DEFAULTS.get(name) == v:
            continue
        out[short] = round(v, 5) if isinstance(v, float) else v
    return out


def decode(row: dict[str, Any]) -> Observation:
    kwargs = {FIELDS[k]: v for k, v in row.items() if k in FIELDS}
    return Observation(**kwargs)


class Recorder:
    def __init__(self, root: Path, retention_days: int = 14) -> None:
        self.root = root
        self.retention_days = retention_days
        self.enabled = True
        self.bytes_written = 0

    def _path(self, ts: float) -> Path:
        dt = datetime.fromtimestamp(ts, UTC)
        return self.root / dt.strftime("%Y-%m-%d") / f"{dt:%H}.ndjson.gz"

    def write(self, batch: SourceBatch) -> None:
        if not self.enabled or not batch.observations:
            return
        path = self._path(batch.fetched_at)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = "".join(
            json.dumps({"b": round(batch.fetched_at, 2), **encode(o)}, separators=(",", ":")) + "\n"
            for o in batch.observations
        ).encode()
        with gzip.open(path, "ab", compresslevel=6) as f:
            f.write(lines)
        self.bytes_written += len(lines)

    def prune(self, now: float) -> int:
        cutoff = (datetime.fromtimestamp(now, UTC) - timedelta(days=self.retention_days)).date()
        removed = 0
        if not self.root.exists():
            return 0
        for d in self.root.iterdir():
            try:
                day = datetime.strptime(d.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if d.is_dir() and day < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        return removed

    def usage(self) -> dict[str, Any]:
        if not self.root.exists():
            return {"days": 0, "bytes": 0}
        days = [d for d in self.root.iterdir() if d.is_dir()]
        size = sum(f.stat().st_size for d in days for f in d.glob("*.gz"))
        return {"days": len(days), "bytes": size, "retention_days": self.retention_days}


def read_batches(root: Path, start: float, end: float) -> Iterator[tuple[float, list[Observation]]]:
    """Yield (fetched_at, observations) per recorded batch between start and end, in order."""
    t = datetime.fromtimestamp(start, UTC).replace(minute=0, second=0, microsecond=0)
    stop = datetime.fromtimestamp(end, UTC)
    while t <= stop:
        path = root / t.strftime("%Y-%m-%d") / f"{t:%H}.ndjson.gz"
        if path.exists():
            current: float | None = None
            obs: list[Observation] = []
            try:
                with gzip.open(path, "rt") as f:
                    for line in f:
                        row = json.loads(line)
                        b = row.pop("b")
                        if b != current:
                            if current is not None and obs and start <= current <= end:
                                yield current, obs
                            current, obs = b, []
                        obs.append(decode(row))
            except (OSError, EOFError, json.JSONDecodeError) as e:
                log.warning("skipping damaged recording %s: %s", path, e)
            if current is not None and obs and start <= current <= end:
                yield current, obs
        t += timedelta(hours=1)
