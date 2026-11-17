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
    def __init__(
        self, message: str, status: int | None = None, retry_after_s: float | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s  # when the source says how long to wait

    @property
    def is_client_error(self) -> bool:
        return self.status is not None and 400 <= self.status < 500


# Sources that are the user's own receiver(s): the `seen_by_my_receiver` filter field.
RECEIVER_SOURCES = ("receiver", "receiver-uat")
# Every live source the app can use, for the `sources` filter field's choices.
KNOWN_SOURCES = ("adsb.fi", "adsb.lol", "flyitalyadsb", "opensky", *RECEIVER_SOURCES)


class AreaSource(Protocol):
    """Answers point/radius queries. `max_rate_per_s` is the published sustained limit (requests
    per second, or credits per second for credit-metered sources); the poller uses at most half."""

    name: str
    max_rate_per_s: float

    async def fetch(self, query: AreaQuery) -> SourceBatch: ...


@runtime_checkable
class MeteredSource(Protocol):
    """An area source whose requests cost a variable number of credits (e.g. by area size)."""

    max_cost: float

    def cost(self, query: AreaQuery) -> float: ...


@runtime_checkable
class FeedSource(Protocol):
    """Returns everything it hears, with no query area and no rate limit (a local receiver)."""

    name: str
    interval_s: float

    async def fetch_all(self) -> SourceBatch: ...


@runtime_checkable
class LookupSource(Protocol):
    """A source that can also find specific aircraft wherever they are (not a global feed)."""

    name: str

    async def fetch_hexes(self, hexes: list[str]) -> SourceBatch: ...

    async def fetch_registration(self, registration: str) -> SourceBatch: ...
