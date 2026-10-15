from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PlaneAlertEntry:
    """A row from plane-alert-db: a curated "interesting" aircraft."""

    icao: str
    registration: str | None
    operator: str | None
    type_desc: str | None
    icao_type: str | None
    cmpg: str | None  # Civ / Mil / Pol / Gov
    tags: tuple[str, ...]
    category: str | None
    link: str | None
    images: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Airline:
    icao: str
    name: str
    callsign_tele: str | None
    country: str | None


@dataclass(slots=True)
class AircraftInfo:
    """Everything known about an airframe from local databases, keyed by ICAO hex."""

    registration: str | None = None
    type_code: str | None = None
    type_desc: str | None = None
    owner_op: str | None = None
    year: str | None = None
    db_military: bool = False
    db_interesting: bool = False
    db_pia: bool = False
    db_ladd: bool = False
    in_military_range: bool = False
    type_military_share: float | None = None
    type_airframes: int = 0
    plane_alert: PlaneAlertEntry | None = None
    extra: dict[str, str] = field(default_factory=dict)
