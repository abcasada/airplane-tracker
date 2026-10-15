from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.enrich.models import AircraftInfo, Airline
from app.sources.base import Observation

HISTORY_POINTS = 40
EXPIRE_AFTER_S = 180.0


@dataclass(slots=True, frozen=True)
class TrackPoint:
    t: float
    lat: float
    lon: float
    alt_ft: float | None
    gs_kt: float | None
    track_deg: float | None
    vrate_fpm: float | None


@dataclass(slots=True)
class Track:
    hex: str
    first_seen: float
    last_seen: float
    source: str = ""
    lat: float | None = None
    lon: float | None = None
    pos_time: float | None = None
    alt_baro_ft: float | None = None
    alt_geom_ft: float | None = None
    on_ground: bool = False
    gs_kt: float | None = None
    track_deg: float | None = None
    baro_rate_fpm: float | None = None
    geom_rate_fpm: float | None = None
    callsign: str | None = None
    squawk: str | None = None
    squawk_streak: int = 0  # consecutive fresh reports with the same squawk
    emergency: str | None = None
    category: str | None = None
    nav_heading_deg: float | None = None
    nav_altitude_ft: float | None = None
    pos_source: str = "other"
    nac_p: int | None = None
    api_registration: str | None = None
    api_type_code: str | None = None
    api_type_desc: str | None = None
    api_owner_op: str | None = None
    api_year: str | None = None
    api_db_flags: int = 0
    updates: int = 0
    history: deque[TrackPoint] = field(default_factory=lambda: deque(maxlen=HISTORY_POINTS))
    info: AircraftInfo | None = None
    airline: Airline | None = None
    airline_callsign: str | None = None  # callsign the airline lookup was done for
    classification: Any = None  # app.classify.military.Classification
    extra: dict[str, Any] = field(default_factory=dict)

    # ---- effective values: aggregator fields first, local databases as fallback ----------------

    @property
    def registration(self) -> str | None:
        return self.api_registration or (self.info.registration if self.info else None)

    @property
    def type_code(self) -> str | None:
        return self.api_type_code or (self.info.type_code if self.info else None)

    @property
    def type_desc(self) -> str | None:
        return self.api_type_desc or (self.info.type_desc if self.info else None)

    @property
    def owner_op(self) -> str | None:
        return self.api_owner_op or (self.info.owner_op if self.info else None)

    @property
    def year(self) -> str | None:
        return self.api_year or (self.info.year if self.info else None)

    @property
    def altitude_ft(self) -> float | None:
        return self.alt_baro_ft if self.alt_baro_ft is not None else self.alt_geom_ft

    @property
    def vertical_rate_fpm(self) -> float | None:
        return self.geom_rate_fpm if self.geom_rate_fpm is not None else self.baro_rate_fpm

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None

    def apply(self, obs: Observation) -> None:
        fresh = obs.seen_at > self.last_seen or self.updates == 0
        self.last_seen = max(self.last_seen, obs.seen_at)
        self.source = obs.source
        self.updates += 1
        if obs.pos_time is not None and (self.pos_time is None or obs.pos_time > self.pos_time):
            self.lat, self.lon, self.pos_time = obs.lat, obs.lon, obs.pos_time
            self.pos_source = obs.pos_source
            if obs.lat is not None and obs.lon is not None:
                self.history.append(
                    TrackPoint(
                        obs.pos_time,
                        obs.lat,
                        obs.lon,
                        obs.alt_baro_ft if obs.alt_baro_ft is not None else obs.alt_geom_ft,
                        obs.gs_kt,
                        obs.track_deg,
                        obs.geom_rate_fpm if obs.geom_rate_fpm is not None else obs.baro_rate_fpm,
                    )
                )
        self.on_ground = obs.on_ground
        if obs.on_ground:
            self.alt_baro_ft = None
        elif obs.alt_baro_ft is not None:
            self.alt_baro_ft = obs.alt_baro_ft
        for attr in (
            "alt_geom_ft", "gs_kt", "track_deg", "baro_rate_fpm", "geom_rate_fpm",
            "category", "nav_heading_deg", "nav_altitude_ft", "nac_p",
        ):  # fmt: skip
            v = getattr(obs, attr)
            if v is not None:
                setattr(self, attr, v)
        if obs.callsign:
            self.callsign = obs.callsign
        if fresh:
            if obs.squawk and obs.squawk == self.squawk:
                self.squawk_streak += 1
            else:
                self.squawk_streak = 1 if obs.squawk else 0
            self.squawk = obs.squawk
            self.emergency = obs.emergency
        if obs.registration:
            self.api_registration = obs.registration
        if obs.type_code:
            self.api_type_code = obs.type_code
        if obs.type_desc:
            self.api_type_desc = obs.type_desc
        if obs.owner_op:
            self.api_owner_op = obs.owner_op
        if obs.year:
            self.api_year = obs.year
        if obs.db_flags:
            self.api_db_flags = obs.db_flags


class TrackStore:
    def __init__(self) -> None:
        self.tracks: dict[str, Track] = {}

    def update(self, obs: Observation) -> tuple[Track, bool]:
        track = self.tracks.get(obs.hex)
        is_new = track is None
        if track is None:
            track = Track(hex=obs.hex, first_seen=obs.seen_at, last_seen=obs.seen_at)
            self.tracks[obs.hex] = track
        track.apply(obs)
        return track, is_new

    def expire(self, now: float, max_age_s: float = EXPIRE_AFTER_S) -> list[str]:
        gone = [h for h, t in self.tracks.items() if now - t.last_seen > max_age_s]
        for h in gone:
            del self.tracks[h]
        return gone

    def get(self, hex_: str) -> Track | None:
        return self.tracks.get(hex_)

    def __len__(self) -> int:
        return len(self.tracks)

    def values(self) -> list[Track]:
        return list(self.tracks.values())
