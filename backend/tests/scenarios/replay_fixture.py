"""Synthetic recording for replay tests: one hour of 10 s batches around generic example places.

Regenerate with `uv run python -m tests.scenarios.replay_fixture` (deterministic). If the output
changes, review tests/fixtures/replay/synthetic-1.expected.json by hand before updating it.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app import geo
from app.ingest.recorder import Recorder
from app.sources.base import Observation, SourceBatch
from tests.scenarios.flight import T0, FlightState, at

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "replay"
NAME = "synthetic-1"
DURATION_S = 1800.0
STEP_S = 10.0

HOME = (38.84, -97.61)
RANGE_CENTER = (38.67, -97.85)
KSLN = (38.791, -97.652)


@dataclass
class Flight:
    hex: str
    start_s: float
    duration_s: float
    state: FlightState
    callsign: str | None = None
    type_code: str | None = None
    db_flags: int = 0
    # (time since start, turn rate dps) changes
    maneuvers: list[tuple[float, float]] = field(default_factory=list)


def flights() -> list[Flight]:
    return [
        # Military heavy straight over home from the south: a visible event at ~5 min.
        Flight("ae1001", 0, 700, FlightState(*at(*HOME, 180, 25), 9220, 250, 0, category="A5"),
               "RCH452", "C17", db_flags=1),
        # Parallel track 6.8 nm east at 8,000 ft: never visible (too low on the horizon).
        Flight("a2002", 60, 600, FlightState(*at(*at(*HOME, 90, 6.8), 180, 25), 9220, 250, 0),
               "SWA123", "B738"),
        # Orbiting 4 nm west: close but never visible, mustn't trigger.
        Flight("a3003", 0, 900, FlightState(*at(*HOME, 270, 4), 3000, 100, 0, turn_dps=3,
               category="A1"), "N123AB", "C172"),
        # Inbound from the north, then turns around at ~11 nm: a false alarm for any predictor.
        Flight("a4004", 300, 600, FlightState(*at(*HOME, 0, 20), 9220, 250, 180),
               "N44DD", "BE20", maneuvers=[(130, 3.0), (190, 0.0)]),
        # Straight-in final to KSLN runway 35, descending.
        Flight("a5005", 600, 200, FlightState(*at(*KSLN, 180, 9), 4000, 160, 0, vrate_fpm=-700),
               "EJA512", "C68A"),
        # Military jet inbound to the range from the east at 15,000 ft.
        Flight("ae6006", 900, 400, FlightState(*at(*RANGE_CENTER, 90, 22), 15000, 300, 270),
               "HAWK11", "F16", db_flags=1),
        # Appears already overhead: visible on first sight, so nothing could have predicted it.
        Flight("a7007", 1200, 300, FlightState(*at(*HOME, 0, 0.3), 6220, 200, 90),
               "N77GG", "PC12"),
    ]  # fmt: skip


def observations_at(t_rel: float, fl: list[Flight]) -> list[Observation]:
    out = []
    for f in fl:
        dt = t_rel - f.start_s
        if dt < 0 or dt > f.duration_s:
            continue
        s = f.state
        now = T0 + t_rel
        out.append(
            Observation(
                hex=f.hex, source="adsb.lol", seen_at=now, pos_time=now, lat=round(s.lat, 5),
                lon=round(s.lon, 5), alt_baro_ft=round(s.alt_ft), gs_kt=s.gs_kt,
                track_deg=round(s.track_deg % 360, 1), baro_rate_fpm=s.vrate_fpm,
                category=s.category, pos_source="adsb", callsign=f.callsign,
                type_code=f.type_code, db_flags=f.db_flags,
            )
        )  # fmt: skip
        for at_s, turn in f.maneuvers:
            if dt == at_s:
                s.turn_dps = turn
        mid = s.track_deg + s.turn_dps * STEP_S / 2
        s.lat, s.lon = geo.destination(s.lat, s.lon, mid % 360, s.gs_kt * STEP_S / 3600.0)
        s.track_deg = (s.track_deg + s.turn_dps * STEP_S) % 360
        s.alt_ft += s.vrate_fpm * STEP_S / 60.0
    return out


def generate(root: Path) -> None:
    if root.exists():
        shutil.rmtree(root)
    rec = Recorder(root)
    fl = flights()
    t = 0.0
    while t <= DURATION_S:
        rec.write(SourceBatch("adsb.lol", T0 + t, observations_at(t, fl)))
        t += STEP_S


if __name__ == "__main__":
    generate(FIXTURES / NAME)
    print(f"wrote {FIXTURES / NAME}")
