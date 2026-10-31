from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

PosSource = Literal["adsb", "mlat", "tisb", "other"]


@dataclass(slots=True)
class Observation:
    """One aircraft report, normalized. Units: ft, kt, ft/min, degrees true, UTC epoch s."""

    hex: str
    source: str
    seen_at: float  # when the source last heard any message from it
    pos_time: float | None = None  # when the position was measured
    lat: float | None = None
    lon: float | None = None
    alt_baro_ft: float | None = None
    alt_geom_ft: float | None = None
    on_ground: bool = False
    gs_kt: float | None = None
    track_deg: float | None = None
    baro_rate_fpm: float | None = None
    geom_rate_fpm: float | None = None
    callsign: str | None = None
    squawk: str | None = None
    emergency: str | None = None
    category: str | None = None
    nav_heading_deg: float | None = None
    nav_altitude_ft: float | None = None
    pos_source: PosSource = "other"
    nac_p: int | None = None
    # Database fields some aggregators include.
    registration: str | None = None
    type_code: str | None = None
    type_desc: str | None = None
    owner_op: str | None = None
    year: str | None = None
    db_flags: int = 0


@dataclass(frozen=True, slots=True)
class AreaQuery:
    lat: float
    lon: float
    radius_nm: float


@dataclass(slots=True)
class SourceBatch:
    source: str
    fetched_at: float
    observations: list[Observation] = field(default_factory=list)


class SourceError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def is_client_error(self) -> bool:
        return self.status is not None and 400 <= self.status < 500


class AreaSource(Protocol):
    name: str
    max_rate_per_s: float  # the published limit; the poller uses at most half

    async def fetch(self, query: AreaQuery) -> SourceBatch: ...


@runtime_checkable
class LookupSource(Protocol):
    """A source that can also find specific aircraft wherever they are (not a global feed)."""

    name: str

    async def fetch_hexes(self, hexes: list[str]) -> SourceBatch: ...

    async def fetch_registration(self, registration: str) -> SourceBatch: ...
