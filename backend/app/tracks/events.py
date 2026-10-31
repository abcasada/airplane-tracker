"""Take-off and landing detection from ADS-B reports.

Observed when the aircraft reports the transition (on the ground → airborne or back), which needs
receivers that hear it on the ground. Otherwise inferred:
- take-off: first heard climbing, low, close to an airfield;
- landing: last heard low, close to an airfield and not climbing, then lost.

Pure function of the tracks, the airport index and the injected time, so live alerting and the
replay/backtest share it. State lives in ``track.extra["flight"]``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.places.nearby import Airfield, AirportIndex

from .store import Track

EventKind = Literal["takeoff", "landing"]

FIELD_NEAR_NM = 3.0  # a transition within this distance is credited to the airfield
DEPARTURE_NEAR_NM = 5.0  # first heard this close after take-off, …
DEPARTURE_MAX_AGL_FT = 2500.0  # … this low, …
DEPARTURE_MIN_CLIMB_FPM = 300.0  # … and climbing
ARRIVAL_MAX_AGL_FT = 1000.0  # last heard this low near a field, not climbing, then lost
ARRIVAL_MAX_CLIMB_FPM = 200.0
# Longer than the 60 s lookup interval for followed aircraft, shorter than track expiry (180 s).
LOST_AFTER_S = 150.0
RECENT_S = 600.0


@dataclass(frozen=True, slots=True)
class FlightEvent:
    kind: EventKind
    t: float
    airport_code: str | None
    airport_name: str | None
    inferred: bool

    def describe(self) -> str:
        verb = "took off from" if self.kind == "takeoff" else "landed at"
        if self.airport_code:
            text = f"{verb} {self.airport_name} ({self.airport_code})"
        else:
            text = "took off" if self.kind == "takeoff" else "landed"
        return f"{text} (probably)" if self.inferred and self.kind == "landing" else text


@dataclass(slots=True)
class _Sample:
    lat: float
    lon: float
    alt_ft: float | None
    vrate_fpm: float | None
    t: float


@dataclass(slots=True)
class FlightState:
    last_on_ground: bool | None = None
    last_pos_t: float | None = None
    last_airborne: _Sample | None = None
    last_kind: EventKind | None = None
    lost_checked: bool = False
    events: list[FlightEvent] = field(default_factory=list)


def state_of(track: Track) -> FlightState | None:
    st: FlightState | None = track.extra.get("flight")
    return st


def recent(track: Track, kind: EventKind, now: float) -> FlightEvent | None:
    st = state_of(track)
    if st is None:
        return None
    for ev in reversed(st.events):
        if ev.kind == kind:
            return ev if now - ev.t <= RECENT_S else None
    return None


def _field(airports: AirportIndex, track: Track, lat: float, lon: float, max_nm: float
           ) -> Airfield | None:  # fmt: skip
    # Heliports only for rotorcraft: a heliport next to a runway mustn't take the credit.
    found = airports.airfield_near(lat, lon, max_nm, heliports=track.category == "A7")
    return found[0] if found else None


def _agl(alt_ft: float | None, f: Airfield) -> float | None:
    if alt_ft is None:
        return None
    return alt_ft - (f.elevation_ft or 0.0)


def _event(kind: EventKind, t: float, f: Airfield | None, inferred: bool) -> FlightEvent:
    return FlightEvent(kind, t, f.code if f else None, f.name if f else None, inferred)


def update(
    tracks: list[Track], airports: AirportIndex, now: float
) -> list[tuple[Track, FlightEvent]]:
    """Advance every track's flight state; returns the new events."""
    out: list[tuple[Track, FlightEvent]] = []
    for t in tracks:
        st: FlightState = t.extra.setdefault("flight", FlightState())
        ev = _step(t, st, airports, now)
        if ev is not None and ev.kind != st.last_kind:
            st.last_kind = ev.kind
            st.events = [*st.events[-3:], ev]
            out.append((t, ev))
    return out


def _step(t: Track, st: FlightState, airports: AirportIndex, now: float) -> FlightEvent | None:
    fresh = t.pos_time is not None and (st.last_pos_t is None or t.pos_time > st.last_pos_t)
    if not fresh:
        return _maybe_lost(t, st, airports, now)
    assert t.lat is not None and t.lon is not None and t.pos_time is not None
    first = st.last_pos_t is None
    was_ground = st.last_on_ground
    st.last_on_ground, st.last_pos_t, st.lost_checked = t.on_ground, t.pos_time, False
    if t.on_ground:
        if was_ground is False:
            return _event("landing", t.pos_time, _field(airports, t, t.lat, t.lon, FIELD_NEAR_NM),
                          inferred=False)  # fmt: skip
        return None
    st.last_airborne = _Sample(t.lat, t.lon, t.altitude_ft, t.vertical_rate_fpm, t.pos_time)
    if was_ground is True:
        return _event("takeoff", t.pos_time, _field(airports, t, t.lat, t.lon, FIELD_NEAR_NM),
                      inferred=False)  # fmt: skip
    if first:
        f = _field(airports, t, t.lat, t.lon, DEPARTURE_NEAR_NM)
        agl = _agl(t.altitude_ft, f) if f else None
        climbing = (t.vertical_rate_fpm or 0.0) >= DEPARTURE_MIN_CLIMB_FPM
        if f and agl is not None and agl <= DEPARTURE_MAX_AGL_FT and climbing:
            return _event("takeoff", t.pos_time, f, inferred=True)
    return None


def _maybe_lost(
    t: Track, st: FlightState, airports: AirportIndex, now: float
) -> FlightEvent | None:
    s = st.last_airborne
    if (
        st.lost_checked or st.last_on_ground is not False or s is None
        or t.pos_time is None or now - t.pos_time < LOST_AFTER_S
    ):  # fmt: skip
        return None
    st.lost_checked = True
    f = _field(airports, t, s.lat, s.lon, FIELD_NEAR_NM)
    if f is None:
        return None
    agl = _agl(s.alt_ft, f)
    if agl is None or agl > ARRIVAL_MAX_AGL_FT or (s.vrate_fpm or 0.0) > ARRIVAL_MAX_CLIMB_FPM:
        return None
    return _event("landing", s.t, f, inferred=True)
