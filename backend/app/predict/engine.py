"""Predicts which aircraft will be visible from a viewpoint, enter an area, or arrive at an
airport, with ETA and a confidence score.

Two stages per (aircraft, place):
1. Gate: one deterministic projection (current track + autopilot heading). Pairs that don't come
   near the place within the horizon are skipped cheaply.
2. Ensemble: K sampled trajectories (heading/speed noise, decaying observed turn, maneuver-scaled
   random turn, some samples following the selected heading). p = share that succeed.

confidence = p × data quality × horizon decay. A prediction turns on at ≥ θ on 2 consecutive
evaluations and off after 3 below θ − 0.15 (hysteresis), so turning aircraft don't flicker.
Arrival uses the arrival score (app.predict.arrival) rather than an ensemble.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from app import geo
from app.classify.size import size_for
from app.places.models import Place
from app.tracks.kinematics import Kinematics, derive
from app.tracks.store import Track

from .arrival import ArrivalInputs, Runway, arrival_score

Kind = Literal["visible", "entry", "arrival"]
STANDARD_RATE_DPS = 3.0
HIGH_ALTITUDE_RATE_DPS = 1.5  # jets above FL250 turn more gently
OFF_MARGIN = 0.15
ON_STREAK = 2
OFF_STREAK = 3
PATH_POINTS_EVERY_S = 30.0


class PredictionSettings(BaseModel):
    enabled: bool = True
    horizon_s: float = Field(600.0, ge=60, le=1800)
    lead_max_s: float = Field(600.0, ge=30, le=3600)
    min_confidence: float = Field(0.6, ge=0.05, le=1)
    samples: int = Field(64, ge=8, le=256)
    step_s: float = Field(10.0, ge=2, le=30)


@dataclass(slots=True)
class Prediction:
    kind: Kind
    probability: float
    confidence: float
    eta_s: float | None
    eta_p10_s: float | None = None
    eta_p90_s: float | None = None
    cpa_nm: float | None = None
    max_elevation_deg: float | None = None
    active: bool = False


@dataclass(slots=True)
class _Hysteresis:
    active: bool = False
    up: int = 0
    down: int = 0


@dataclass(slots=True)
class PredictionState:
    """Stored on the Track: latest predictions per place, hysteresis memory and a path for maps."""

    by_place: dict[int, Prediction] = field(default_factory=dict)
    hysteresis: dict[int, _Hysteresis] = field(default_factory=dict)
    path: list[tuple[float, float]] = field(default_factory=list)


def _size_limit(track: Track, place: Place) -> float:
    assert place.visibility is not None
    size = size_for(track.category)
    v = place.visibility
    return {
        "large": v.max_slant_nm_large,
        "medium": v.max_slant_nm_medium,
        "light": v.max_slant_nm_light,
    }[size]


def _effective(place: Place, s: PredictionSettings) -> tuple[float, float]:
    o = place.prediction
    lead = o.lead_max_s if o and o.lead_max_s is not None else s.lead_max_s
    conf = o.min_confidence if o and o.min_confidence is not None else s.min_confidence
    return lead, conf


def _position_quality(track: Track, now: float) -> float:
    q = 1.0
    if track.pos_source in ("mlat", "tisb"):
        q *= 0.85
    if track.pos_time is not None and now - track.pos_time > 15:
        q *= 0.8
    return q


def _data_quality(track: Track, k: Kinematics, now: float) -> float:
    q = _position_quality(track, now)
    # Too little history to know whether it's turning: don't trust a projection yet.
    return min(q, 0.5) if k.history_s < 30 else q


def _horizon_decay(eta_s: float, height_ft: float) -> float:
    tau = 720.0 if height_ft > 5000 else 300.0
    return math.exp(-max(0.0, eta_s) / tau)


@dataclass(slots=True)
class _Sim:
    x: np.ndarray  # east nm, (steps, samples)
    y: np.ndarray  # north nm
    alt: np.ndarray  # ft MSL
    times: np.ndarray

    def shifted(self, dx: float, dy: float) -> _Sim:
        return _Sim(self.x - dx, self.y - dy, self.alt, self.times)


def simulate(
    k: Kinematics,
    track: Track,
    ground_ft: float,
    settings: PredictionSettings,
    samples: int,
    rng: np.random.Generator | None,
) -> _Sim:
    """Trajectories in a local plane centred on the aircraft. samples=1 with rng=None is the
    deterministic projection. Vectorized over time: every term has a closed form."""
    n_steps = int(settings.horizon_s // settings.step_s)
    dt = settings.step_s
    alt0 = track.altitude_ft if track.altitude_ft is not None else ground_ft + 3000.0
    noisy = rng is not None and samples > 1

    heading0 = np.full(samples, k.track_deg)
    gs = np.full(samples, k.gs_kt)
    omega_noise = np.zeros(samples)
    follow_nav = np.zeros(samples, dtype=bool)
    if noisy:
        assert rng is not None
        trk_sigma = 1.0 if track.pos_source == "adsb" else 4.0
        heading0 = heading0 + rng.normal(0.0, trk_sigma, samples)
        gs = gs * (1.0 + rng.normal(0.0, 0.03, samples))
        low_slow = (alt0 - ground_ft) < 5000 or k.gs_kt < 180
        # A sustained random turn rate: small for steady flight (airway course changes), larger
        # for aircraft that have been maneuvering or are low and slow.
        sigma_turn = 0.02 + 1.0 * k.turn_variability_dps + (0.25 if low_slow else 0.0)
        omega_noise = rng.normal(0.0, sigma_turn, samples)
    nav = track.nav_heading_deg
    if nav is not None and abs(geo.angle_diff(nav, k.track_deg)) > 5:
        follow_nav[:] = True
        if noisy:
            assert rng is not None
            # A selected heading that differs from the track means a turn is coming.
            follow_nav = rng.uniform(0.0, 1.0, samples) < 0.9

    times = np.arange(1, n_steps + 1) * dt
    t = times[:, None]
    # Observed turn decays over ~1 min; the random turn persists.
    omega = k.turn_rate_dps * np.exp(-t / 60.0) + omega_noise
    heading = heading0 + np.cumsum(omega, axis=0) * dt
    if follow_nav.any():
        assert nav is not None
        # Turn toward the selected heading at a fixed rate, then hold it.
        nav_rate = HIGH_ALTITUDE_RATE_DPS if alt0 > 25000 else STANDARD_RATE_DPS
        diff = (nav - heading0 + 180.0) % 360.0 - 180.0
        to_nav = heading0 + np.sign(diff) * np.minimum(nav_rate * t, np.abs(diff))
        heading = np.where(follow_nav, to_nav, heading)
    rad = np.radians(heading)
    step_nm = gs * dt / 3600.0
    x = np.cumsum(step_nm * np.sin(rad), axis=0)
    y = np.cumsum(step_nm * np.cos(rad), axis=0)

    v = k.vrate_fpm
    alt = alt0 + v * t / 60.0
    target = track.nav_altitude_ft
    if target is not None and v != 0:
        # Level off at the altitude selected on the autopilot.
        alt = np.minimum(alt, target) if v > 0 else np.maximum(alt, target)
    alt = np.maximum(alt, ground_ft)
    alt = np.broadcast_to(alt, x.shape)
    return _Sim(x, y, alt, times)


class _Projections:
    """One aircraft's projections for this cycle, shared by every place: the deterministic
    path for the gate, and the ensemble only if some place passes the gate."""

    def __init__(
        self,
        k: Kinematics,
        track: Track,
        ground_ft: float,
        settings: PredictionSettings,
        rng: np.random.Generator,
    ) -> None:
        self.k, self.track, self.ground_ft, self.settings, self.rng = (
            k,
            track,
            ground_ft,
            settings,
            rng,
        )
        self._det: _Sim | None = None
        self._ens: _Sim | None = None

    @property
    def det(self) -> _Sim:
        if self._det is None:
            self._det = simulate(self.k, self.track, self.ground_ft, self.settings, 1, None)
        return self._det

    @property
    def ensemble(self) -> _Sim:
        if self._ens is None:
            s = self.settings
            self._ens = simulate(self.k, self.track, self.ground_ft, s, s.samples, self.rng)
        return self._ens


def _closest(sim: _Sim, x0: float, y0: float, alt0: float) -> tuple[np.ndarray, np.ndarray]:
    """Per sample: closest ground distance to the origin along the path (between steps too),
    and the altitude there. Sampling only at steps would miss the true closest point."""
    xs = np.vstack([np.full((1, sim.x.shape[1]), x0), sim.x])
    ys = np.vstack([np.full((1, sim.y.shape[1]), y0), sim.y])
    alts = np.vstack([np.full((1, sim.alt.shape[1]), alt0), sim.alt])
    ax, ay, bx, by = xs[:-1], ys[:-1], xs[1:], ys[1:]
    dx, dy = bx - ax, by - ay
    seg = dx * dx + dy * dy
    u = np.where(seg > 0, np.clip(-(ax * dx + ay * dy) / np.where(seg > 0, seg, 1), 0, 1), 0)
    d = np.hypot(ax + u * dx, ay + u * dy)
    idx = np.argmin(d, axis=0)
    cols = np.arange(d.shape[1])
    alt_at = alts[:-1][idx, cols] + u[idx, cols] * (alts[1:][idx, cols] - alts[:-1][idx, cols])
    return d[idx, cols], alt_at


def _first_success(ok: np.ndarray, times: np.ndarray) -> np.ndarray:
    """Per sample: time of the first True step, or NaN."""
    any_ok = ok.any(axis=0)
    first = np.argmax(ok, axis=0)
    return np.where(any_ok, times[first], np.nan)


def _visible_mask(sim: _Sim, place: Place, limit_nm: float) -> np.ndarray:
    assert place.visibility is not None
    ground = np.hypot(sim.x, sim.y)
    height = sim.alt - place.elevation_ft
    elev = np.degrees(np.arctan2(height, ground * geo.FT_PER_NM))
    slant = np.hypot(ground, np.maximum(height, 0.0) / geo.FT_PER_NM)
    result: np.ndarray = (
        (height > 50) & (elev >= place.visibility.min_elevation_deg) & (slant <= limit_nm)
    )
    return result


def _inside_mask(sim: _Sim, place: Place) -> np.ndarray:
    if place.polygon:
        pts = [geo.to_local(a, b, place.lat, place.lon) for a, b in place.polygon]
        inside = np.zeros(sim.x.shape, dtype=bool)
        j = len(pts) - 1
        for i in range(len(pts)):
            xi, yi = pts[i]
            xj, yj = pts[j]
            if yi != yj:
                crosses = (yi > sim.y) != (yj > sim.y)
                x_cross = (xj - xi) * (sim.y - yi) / (yj - yi) + xi
                inside ^= crosses & (sim.x < x_cross)
            j = i
    else:
        inside = np.hypot(sim.x, sim.y) <= place.radius_nm
    if place.alt_floor_ft is not None:
        inside &= sim.alt >= place.alt_floor_ft
    if place.alt_ceiling_ft is not None:
        inside &= sim.alt <= place.alt_ceiling_ft
    return inside


def _place_reach_nm(place: Place, track: Track) -> float:
    if place.role == "viewpoint":
        return _size_limit(track, place)
    if place.polygon:
        return max(geo.haversine_nm(place.lat, place.lon, a, b) for a, b in place.polygon)
    return place.radius_nm


def predict_pair(
    track: Track,
    k: Kinematics,
    place: Place,
    settings: PredictionSettings,
    now: float,
    proj: _Projections,
    runways: list[Runway] | None = None,
) -> Prediction | None:
    if place.role == "airport":
        return _predict_arrival(track, k, place, now, runways or [])
    kind: Kind = "visible" if place.role == "viewpoint" else "entry"
    if kind == "visible" and place.visibility is None:
        return None
    assert track.lat is not None and track.lon is not None
    reach = _place_reach_nm(place, track)
    dist_now = geo.haversine_nm(place.lat, place.lon, track.lat, track.lon)
    if dist_now - reach > k.gs_kt * settings.horizon_s / 3600.0:
        return None  # can't get there within the horizon at this speed

    ground = place.elevation_ft
    limit = _size_limit(track, place) if kind == "visible" else 0.0

    def success(sim: _Sim) -> np.ndarray:
        return _visible_mask(sim, place, limit) if kind == "visible" else _inside_mask(sim, place)

    height = (track.altitude_ft or ground) - ground
    q_data = _data_quality(track, k, now)
    x0, y0 = geo.to_local(track.lat, track.lon, place.lat, place.lon)
    alt0 = track.altitude_ft if track.altitude_ft is not None else ground
    here = _Sim(np.array([[x0]]), np.array([[y0]]), np.array([[alt0]]), np.array([0.0]))
    if success(here).any():
        # Observed, not projected: only position quality matters.
        q_pos = _position_quality(track, now)
        pred = Prediction(kind, 1.0, round(q_pos, 3), 0.0, 0.0, 0.0, round(math.hypot(x0, y0), 2))
        if kind == "visible":
            pred.max_elevation_deg = round(
                math.degrees(math.atan2(alt0 - ground, math.hypot(x0, y0) * geo.FT_PER_NM)), 1
            )
        return pred

    # Place position in the aircraft-centred plane of the shared projections.
    px, py = -x0, -y0
    # Stage 1: deterministic gate with a relaxed target.
    det = proj.det.shifted(px, py)
    min_d = float(np.min(np.hypot(det.x, det.y)))
    if min_d > reach * 2 + 3 and not success(det).any():
        return None

    sims = proj.ensemble.shifted(px, py)
    ok = success(sims)
    first = _first_success(ok, sims.times)
    hit = ~np.isnan(first)
    p = float(hit.mean())
    cpa_d, cpa_alt = _closest(sims, x0, y0, alt0)
    cpa = round(float(np.median(cpa_d)), 2)
    if not hit.any():
        return Prediction(kind, 0.0, 0.0, None, cpa_nm=cpa)
    eta = float(np.median(first[hit]))
    pred = Prediction(
        kind=kind,
        probability=round(p, 3),
        confidence=round(p * q_data * _horizon_decay(eta, height), 3),
        eta_s=round(eta, 1),
        eta_p10_s=round(float(np.percentile(first[hit], 10)), 1),
        eta_p90_s=round(float(np.percentile(first[hit], 90)), 1),
        cpa_nm=cpa,
    )
    if kind == "visible":
        elev = np.degrees(np.arctan2(cpa_alt - place.elevation_ft, cpa_d * geo.FT_PER_NM))
        pred.max_elevation_deg = round(float(np.median(elev)), 1)
    return pred


def _turn_stability(k: Kinematics) -> float:
    """The arrival score reads the current heading, which means little mid-turn or while
    maneuvering: 1 when steady, ~0.8 at 1°/s, ~0.3 at 2°/s, ~0.14 at 3°/s."""
    rate = max(abs(k.turn_rate_dps), k.turn_variability_dps) - 0.5
    return 1.0 if rate <= 0 else 1.0 / (1.0 + rate * rate)


def _predict_arrival(
    track: Track, k: Kinematics, place: Place, now: float, runways: list[Runway]
) -> Prediction | None:
    assert track.lat is not None and track.lon is not None
    height = (track.altitude_ft or 0.0) - place.elevation_ft
    score, rwy = arrival_score(
        ArrivalInputs(track.lat, track.lon, height, k.gs_kt, k.track_deg, k.vrate_fpm, False),
        place.lat,
        place.lon,
        runways,
    )
    if score <= 0:
        return None
    dist = geo.haversine_nm(place.lat, place.lon, track.lat, track.lon)
    # Not lined up yet: allow a few miles for maneuvering onto final or into the pattern.
    path_nm = dist + (0.0 if rwy else 3.0)
    eta = path_nm / max(k.gs_kt, 60.0) * 3600.0
    conf = score * _data_quality(track, k, now) * (1.0 if rwy else _turn_stability(k))
    return Prediction("arrival", score, round(conf, 3), round(eta, 1), cpa_nm=round(dist, 2))


def _apply_hysteresis(
    state: PredictionState, place_id: int, pred: Prediction | None, lead_s: float, conf_min: float
) -> None:
    h = state.hysteresis.setdefault(place_id, _Hysteresis())
    on = (
        pred is not None
        and pred.eta_s is not None
        and pred.eta_s <= lead_s
        and (pred.confidence >= conf_min)
    )
    clearly_off = (
        pred is None
        or pred.eta_s is None
        or pred.eta_s > lead_s + 60
        or pred.confidence < conf_min - OFF_MARGIN
    )
    already_there = pred is not None and pred.eta_s is not None and pred.eta_s <= 0
    if not h.active:
        h.up = h.up + 1 if on else 0
        if already_there or h.up >= ON_STREAK:
            h.active, h.down = True, 0
    else:
        h.down = h.down + 1 if clearly_off else 0
        if h.down >= OFF_STREAK:
            h.active, h.up = False, 0
    if pred is not None:
        pred.active = h.active


def deterministic_path(track: Track, proj: _Projections) -> list[tuple[float, float]]:
    assert track.lat is not None and track.lon is not None
    origin = (track.lat, track.lon)
    sim = proj.det
    every = max(1, int(PATH_POINTS_EVERY_S // proj.settings.step_s))
    pts = [origin]
    for i in range(every - 1, len(sim.times), every):
        pts.append(geo.from_local(float(sim.x[i, 0]), float(sim.y[i, 0]), *origin))
    return [(round(a, 5), round(b, 5)) for a, b in pts]


class Predictor:
    def __init__(
        self, settings: PredictionSettings | None = None, seed: int = 7, paths: bool = True
    ) -> None:
        self.settings = settings or PredictionSettings()
        self.rng = np.random.default_rng(seed)
        self.paths = paths  # map polylines; replay doesn't need them

    def update(
        self,
        tracks: list[Track],
        places: list[Place],
        now: float,
        runways: dict[int, list[Runway]],
    ) -> None:
        s = self.settings
        active_places = [p for p in places if p.is_active(now)]
        # One terrain floor for the shared projections: places in reach are in one region.
        ground = min((p.elevation_ft for p in active_places), default=0.0)
        for t in tracks:
            state: PredictionState = t.extra.setdefault("prediction", PredictionState())
            k = derive(t) if s.enabled else None
            proj = _Projections(k, t, ground, s, self.rng) if k is not None else None
            state.path = deterministic_path(t, proj) if proj is not None and self.paths else []
            for p in active_places:
                pred = (
                    predict_pair(t, k, p, s, now, proj, runways.get(p.id))
                    if k is not None and proj is not None
                    else None
                )
                lead, conf = _effective(p, s)
                _apply_hysteresis(state, p.id, pred, lead, conf)
                if pred is None:
                    state.by_place.pop(p.id, None)
                else:
                    state.by_place[p.id] = pred


def prediction_for(track: Track, place_id: int) -> Prediction | None:
    state: PredictionState | None = track.extra.get("prediction")
    return state.by_place.get(place_id) if state else None


def is_active(track: Track, place_id: int) -> bool:
    state: PredictionState | None = track.extra.get("prediction")
    if state is None:
        return False
    h = state.hysteresis.get(place_id)
    return bool(h and h.active)
