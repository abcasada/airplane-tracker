from typing import Literal

from pydantic import BaseModel, Field, model_validator

PlaceRole = Literal["viewpoint", "airport", "area"]


class Visibility(BaseModel):
    """When an aircraft counts as visible from a viewpoint."""

    min_elevation_deg: float = Field(15.0, ge=0, le=90)
    max_slant_nm_large: float = Field(20.0, gt=0, le=100)
    max_slant_nm_medium: float = Field(12.0, gt=0, le=100)
    max_slant_nm_light: float = Field(5.0, gt=0, le=100)


class Schedule(BaseModel):
    """Weekly active window in the place's local time, e.g. restricted-area hours."""

    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])  # Monday = 0
    start: str = Field("00:00", pattern=r"^\d{2}:\d{2}$")
    end: str = Field("24:00", pattern=r"^\d{2}:\d{2}$")
    tz: str = "America/Chicago"


class PlaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    role: PlaceRole = "viewpoint"
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    elevation_ft: float = Field(0.0, ge=-1500, le=30000)
    radius_nm: float = Field(10.0, gt=0, le=250)
    polygon: list[tuple[float, float]] | None = Field(None, max_length=500)  # (lat, lon)
    alt_floor_ft: float | None = None
    alt_ceiling_ft: float | None = None
    airport_icao: str | None = Field(None, max_length=8)
    visibility: Visibility | None = None
    active_from: float | None = None
    active_until: float | None = None
    schedule: Schedule | None = None
    is_home: bool = False
    enabled: bool = True
    source: Literal["manual", "geolocation", "airport_lookup"] = "manual"

    @model_validator(mode="after")
    def _check(self) -> "PlaceIn":
        if self.role == "viewpoint" and self.visibility is None:
            self.visibility = Visibility()
        if self.polygon is not None and len(self.polygon) < 3:
            raise ValueError("A polygon needs at least 3 points")
        if self.is_home and self.role != "viewpoint":
            raise ValueError("Only a viewpoint can be home")
        if self.airport_icao:
            self.airport_icao = self.airport_icao.strip().upper()
        return self


class Place(PlaceIn):
    id: int
    created_at: float
    updated_at: float

    def is_active(self, now: float) -> bool:
        if not self.enabled:
            return False
        if self.active_from is not None and now < self.active_from:
            return False
        return not (self.active_until is not None and now > self.active_until)
