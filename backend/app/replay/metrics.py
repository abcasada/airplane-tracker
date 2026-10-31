"""Prediction metrics against ground truth taken from the recording's own future.

For each (aircraft, place) pair, an *event* is the moment the aircraft actually becomes visible
from a viewpoint, enters an area, or reaches an airport. An *alarm* is the moment a prediction
turns on while the event hasn't happened yet. An alarm is true if the event follows within the
lead window; otherwise it's a false alarm. The baseline is adsb-alarm's heuristic: heading within
90° of the bearing to the place and getting closer (https://github.com/stfnhh/adsb-alarm).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from app import geo
from app.filters.facts import LiveFacts
from app.places.models import Place
from app.predict.engine import Kind, is_active, prediction_for

from .core import Cycle

GRACE_S = 120.0  # an alarm may be this late relative to its lead window and still count
MERGE_S = 120.0  # flicker: a new onset this soon after the last one is the same event
MIN_TRACKED_S = 60.0  # events seen less than this after first contact can't be predicted
CALIBRATION_EVERY_S = 30.0
ARRIVAL_RADIUS_NM = 2.0
ARRIVAL_HEIGHT_FT = 1000.0
SOURCES = ("predictor", "baseline")


def kind_for(place: Place) -> Kind:
    return {"viewpoint": "visible", "area": "entry", "airport": "arrival"}[place.role]  # type: ignore[return-value]


def actual(f: LiveFacts, place: Place) -> bool:
    t = f.track
    if not t.has_position:
        return False
    if place.role == "viewpoint":
        return bool(f.get("visible_now", place.id))
    if place.role == "area":
        return bool(f.get("inside_area", place.id))
    assert t.lat is not None and t.lon is not None
    dist = geo.haversine_nm(place.lat, place.lon, t.lat, t.lon)
    if t.on_ground:
        return dist <= ARRIVAL_RADIUS_NM * 1.5
    alt = t.altitude_ft
    return (
        dist <= ARRIVAL_RADIUS_NM
        and alt is not None
        and alt - place.elevation_ft < ARRIVAL_HEIGHT_FT
    )


def baseline(f: LiveFacts, place: Place, lead_max_s: float) -> tuple[bool, float | None]:
    """adsb-alarm: pointed within 90° of the place, closing, and able to get there in time."""
    t = f.track
    if not t.has_position or t.on_ground or t.gs_kt is None or t.track_deg is None or t.gs_kt < 30:
        return False, None
    assert t.lat is not None and t.lon is not None
    dist = geo.haversine_nm(t.lat, t.lon, place.lat, place.lon)
    brg = geo.bearing_deg(t.lat, t.lon, place.lat, place.lon)
    if abs(geo.angle_diff(t.track_deg, brg)) > 90:
        return False, None
    reach = place.radius_nm if place.role == "area" else 0.0
    eta = max(0.0, dist - reach) / t.gs_kt * 3600.0
    return eta <= lead_max_s, eta


@dataclass
class _Alarm:
    t: float
    eta_s: float | None


@dataclass
class _Pair:
    last_t: float = 0.0
    last_actual_t: float | None = None
    actual: bool = False
    on: dict[str, bool] = field(default_factory=lambda: dict.fromkeys(SOURCES, False))
    pending: dict[str, list[_Alarm]] = field(default_factory=lambda: {s: [] for s in SOURCES})
    calib: list[tuple[float, float]] = field(default_factory=list)  # (t, confidence)
    last_calib_t: float = -1e18


@dataclass
class Tally:
    alarms: int = 0
    true_alarms: int = 0
    false_alarms: int = 0
    events: int = 0
    predictable: int = 0
    hits: int = 0
    leads: list[float] = field(default_factory=list)
    eta_errors: list[float] = field(default_factory=list)

    def summary(self, days: float) -> dict[str, Any]:
        resolved = self.true_alarms + self.false_alarms
        return {
            "alarms": self.alarms,
            "true_alarms": self.true_alarms,
            "false_alarms": self.false_alarms,
            "false_alarms_per_day": round(self.false_alarms / days, 2) if days > 0 else None,
            "precision": round(self.true_alarms / resolved, 3) if resolved else None,
            "events": self.events,
            "predictable_events": self.predictable,
            "recall": round(self.hits / self.predictable, 3) if self.predictable else None,
            "lead_median_s": round(statistics.median(self.leads)) if self.leads else None,
            "lead_p10_s": round(_quantile(self.leads, 0.1)) if self.leads else None,
            "eta_error_median_s": (
                round(statistics.median(self.eta_errors)) if self.eta_errors else None
            ),
        }


def _quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


class MetricsCollector:
    def __init__(self, places: list[Place], lead_max_s: float) -> None:
        self.places = places
        self.lead_max_s = lead_max_s
        self.window_s = lead_max_s + GRACE_S
        self.pairs: dict[tuple[str, int], _Pair] = {}
        self.tallies: dict[tuple[Kind, str], Tally] = {}
        self.calibration: dict[Kind, list[tuple[float, bool]]] = {}
        self.start: float | None = None
        self.end: float | None = None
        self._last_sweep = 0.0

    def _tally(self, kind: Kind, source: str) -> Tally:
        return self.tallies.setdefault((kind, source), Tally())

    def observe(self, cycle: Cycle) -> None:
        now = cycle.now
        self.start = now if self.start is None else self.start
        self.end = now
        active = [p for p in self.places if p.is_active(now)]
        for f in cycle.facts:
            if not f.track.has_position:
                continue
            for p in active:
                self._observe_pair(f, p, now)
        if now - self._last_sweep >= 60:
            self._sweep(now)
            self._last_sweep = now

    def _observe_pair(self, f: LiveFacts, place: Place, now: float) -> None:
        t = f.track
        key = (t.hex, place.id)
        kind = kind_for(place)
        is_actual = actual(f, place)
        pred = prediction_for(t, place.id)
        pred_on = is_active(t, place.id)
        base_on, base_eta = baseline(f, place, self.lead_max_s)
        pair = self.pairs.get(key)
        if pair is None:
            if not (is_actual or pred_on or base_on or pred is not None):
                return
            pair = self.pairs[key] = _Pair(last_t=now, actual=is_actual)
            # Already there on first sight: not an onset we could have predicted.
            if is_actual:
                pair.last_actual_t = now
        else:
            if is_actual and not pair.actual:
                self._onset(pair, kind, now, now - t.first_seen)
            pair.actual = is_actual
            if is_actual:
                pair.last_actual_t = now
        pair.last_t = now

        for source, on, eta in (
            ("predictor", pred_on, pred.eta_s if pred else None),
            ("baseline", base_on, base_eta),
        ):
            if on and not pair.on[source] and not is_actual:
                pair.pending[source].append(_Alarm(now, eta))
                self._tally(kind, source).alarms += 1
            pair.on[source] = on

        if pred is not None and not is_actual and now - pair.last_calib_t >= CALIBRATION_EVERY_S:
            pair.calib.append((now, pred.confidence))
            pair.last_calib_t = now

    def _onset(self, pair: _Pair, kind: Kind, now: float, tracked_for: float) -> None:
        # A flicker (back within MERGE_S) settles pending alarms but isn't a new event.
        new_event = pair.last_actual_t is None or now - pair.last_actual_t >= MERGE_S
        for source in SOURCES:
            tally = self._tally(kind, source)
            alarms = [a for a in pair.pending[source] if now - a.t <= self.window_s]
            tally.true_alarms += len(alarms)
            tally.false_alarms += len(pair.pending[source]) - len(alarms)
            pair.pending[source] = []
            if not new_event:
                continue
            tally.events += 1
            if tracked_for >= MIN_TRACKED_S:
                tally.predictable += 1
                if alarms:
                    tally.hits += 1
            if alarms:
                first = alarms[0]
                tally.leads.append(now - first.t)
                if first.eta_s is not None:
                    tally.eta_errors.append(abs(first.eta_s - (now - first.t)))
        cal = self.calibration.setdefault(kind, [])
        for t, conf in pair.calib:
            cal.append((conf, now - t <= self.lead_max_s))
        pair.calib = []

    def _expire(self, pair: _Pair, kind: Kind, now: float) -> None:
        for source in SOURCES:
            stale = [a for a in pair.pending[source] if now - a.t > self.window_s]
            if stale:
                self._tally(kind, source).false_alarms += len(stale)
                pair.pending[source] = [a for a in pair.pending[source] if a not in stale]
        cal = self.calibration.setdefault(kind, [])
        keep: list[tuple[float, float]] = []
        for t, conf in pair.calib:
            if now - t > self.lead_max_s:
                cal.append((conf, False))
            else:
                keep.append((t, conf))
        pair.calib = keep

    def _sweep(self, now: float) -> None:
        by_id = {p.id: p for p in self.places}
        for key, pair in list(self.pairs.items()):
            place = by_id.get(key[1])
            if place is None:
                continue
            self._expire(pair, kind_for(place), now)
            idle = now - pair.last_t > self.window_s
            if idle and not pair.calib and not any(pair.pending.values()):
                del self.pairs[key]

    def report(self) -> dict[str, Any]:
        """Alarms still inside their window when the recording ends are left out (unknown)."""
        if self.end is not None:
            for key, pair in self.pairs.items():
                place = next((p for p in self.places if p.id == key[1]), None)
                if place is not None:
                    self._expire(pair, kind_for(place), self.end)
        days = ((self.end or 0) - (self.start or 0)) / 86400
        out: dict[str, Any] = {"days": round(days, 3), "lead_max_s": self.lead_max_s, "kinds": {}}
        for kind in sorted({k for k, _ in self.tallies}):
            out["kinds"][kind] = {
                source: self.tallies.get((kind, source), Tally()).summary(days)
                for source in SOURCES
            }
        for kind, samples in self.calibration.items():
            out["kinds"].setdefault(kind, {})["calibration"] = calibration(samples)
        return out


def calibration(samples: list[tuple[float, bool]], bins: int = 10) -> dict[str, Any]:
    """Brier score and expected calibration error over (confidence, outcome) samples."""
    if not samples:
        return {"samples": 0, "brier": None, "ece": None, "bins": []}
    brier = sum((c - float(o)) ** 2 for c, o in samples) / len(samples)
    table = []
    ece = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [(c, o) for c, o in samples if lo <= c < hi or (b == bins - 1 and c == 1.0)]
        if not sel:
            continue
        mean_c = sum(c for c, _ in sel) / len(sel)
        rate = sum(1 for _, o in sel if o) / len(sel)
        ece += len(sel) / len(samples) * abs(mean_c - rate)
        table.append({"from": lo, "to": hi, "n": len(sel), "confidence": round(mean_c, 3),
                      "observed": round(rate, 3)})  # fmt: skip
    return {"samples": len(samples), "brier": round(brier, 4), "ece": round(ece, 4), "bins": table}
