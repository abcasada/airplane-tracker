"""Replay recorded traffic and report prediction metrics vs the adsb-alarm baseline.

    DATA_DIR=/data python -m app.replay --days 3
    python -m app.replay --from 2026-10-01 --days 7 --json

Uses the places, filters and settings stored in DATA_DIR. Reads only: nothing is written.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.auth.service import AuthService
from app.auth.throttle import LoginThrottle
from app.clock import SystemClock
from app.config import Settings
from app.context import AppContext
from app.db import Database

from .runner import evaluate_recordings


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.replay", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    p.add_argument(
        "--from", dest="start", help="UTC date or time, e.g. 2026-10-01 or 2026-10-01T08:00"
    )
    p.add_argument("--days", type=float, default=1.0)
    p.add_argument(
        "--recordings", type=Path, help="recordings folder (default DATA_DIR/recordings)"
    )
    p.add_argument("--lead", type=float, help="override lead time (s)")
    p.add_argument("--confidence", type=float, help="override minimum confidence (0-1)")
    p.add_argument("--json", action="store_true", help="print the full report as JSON")
    return p.parse_args(argv)


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.3g}"
    return str(v)


def print_report(report: dict[str, Any]) -> None:
    pred = report["prediction"]
    print(f"{report['batches']} batches, {pred['days']} days, lead window {pred['lead_max_s']:g} s")
    cols = {
        "alarms": "alarms", "false_alarms_per_day": "FA/day", "precision": "precision",
        "predictable_events": "events", "recall": "recall", "lead_median_s": "lead med s",
        "lead_p10_s": "lead p10 s", "eta_error_median_s": "ETA err s",
    }  # fmt: skip
    for kind, by_source in pred["kinds"].items():
        print(f"\n{kind}")
        print("  " + "source".ljust(10) + "".join(h.rjust(11) for h in cols.values()))
        for source in ("predictor", "baseline"):
            row = by_source.get(source)
            if row:
                print("  " + source.ljust(10) + "".join(_fmt(row[c]).rjust(11) for c in cols))
        cal = by_source.get("calibration")
        if cal and cal["samples"]:
            print(
                f"  calibration: {cal['samples']} samples, Brier {cal['brier']}, ECE {cal['ece']}"
            )
    if report["filters"]:
        print("\nfilters")
        for f in report["filters"]:
            counts = f"{f.get('episodes', 0):>6} episodes {f.get('notified', 0):>6} notified"
            print(f"  {f['name'][:40].ljust(40)} {counts} {f.get('aircraft', 0):>6} aircraft")


async def main(argv: list[str] | None = None) -> int:
    from app.runtime import Runtime

    args = _parse(argv)
    cfg = Settings()
    clock = SystemClock()
    end = clock.now()
    start = end - args.days * 86400
    if args.start:
        start = datetime.fromisoformat(args.start).replace(tzinfo=UTC).timestamp()
        end = start + args.days * 86400
    db = Database(cfg.db_path)
    await db.connect()
    http = httpx.AsyncClient()  # never used: replay makes no network calls
    try:
        ctx = AppContext(cfg, db, clock, AuthService(db, clock, cfg.session_days),
                         LoginThrottle(), http, "")  # fmt: skip
        rt = Runtime(ctx)
        await rt.reload_places()
        await rt.reload_classifier()
        await rt.reload_prediction()
        await rt.reload_filters()
        await rt.enricher.reload()
        await rt.routes.load()
        await rt.alerts.load()
        settings = rt.predictor.settings.model_copy()
        if args.lead is not None:
            settings.lead_max_s = args.lead
        if args.confidence is not None:
            settings.min_confidence = args.confidence
        root = args.recordings or rt.recorder.root
        report = await evaluate_recordings(rt, root, start, end, settings)
    finally:
        await http.aclose()
        await db.close()
    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
