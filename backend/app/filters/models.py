from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

Op = Literal[
    "lt", "le", "gt", "ge", "between", "eq", "ne", "exists", "not_exists",
    "in", "not_in", "glob", "not_glob", "contains", "regex",
    "is_true", "is_false", "contains_any", "contains_all", "contains_none",
]  # fmt: skip


class Condition(BaseModel):
    type: Literal["cond"] = "cond"
    id: str = Field(default="", max_length=40)
    field: str = Field(max_length=60)
    op: Op
    value: Any = None
    place: int | None = None
    enabled: bool = True


class Group(BaseModel):
    type: Literal["group"] = "group"
    id: str = Field(default="", max_length=40)
    mode: Literal["all", "any", "none"] = "all"
    children: list[Annotated[Condition | Group, Field(discriminator="type")]] = Field(
        default_factory=list, max_length=60
    )
    enabled: bool = True


class QuietHours(BaseModel):
    start: str = Field("22:00", pattern=r"^\d{2}:\d{2}$")
    end: str = Field("07:00", pattern=r"^\d{2}:\d{2}$")


class NotifySettings(BaseModel):
    """How matches reach the phone. All suppression options default to off."""

    enabled: bool = True
    # What to notify about for matched aircraft (take-off/landing: app.tracks.events).
    on_match: bool = True
    on_takeoff: bool = False
    on_landing: bool = False
    priority: Literal["low", "normal", "high", "critical"] = "high"
    channel: str = Field("Aircraft", max_length=60)  # Android notification channel
    alarm_stream: bool = False  # sound even in Do Not Disturb
    include_photo: bool = True
    update_eta: bool = True  # update the notification in place when the ETA shifts
    title_template: str | None = Field(None, max_length=300)
    message_template: str | None = Field(None, max_length=1000)
    # Optional suppression; None means off.
    repeat_suppress_min: float | None = Field(None, ge=0, le=10080)
    max_per_day: int | None = Field(None, ge=1, le=1000)
    quiet_hours: QuietHours | None = None


class FilterDefinition(BaseModel):
    root: Group = Field(default_factory=Group)
    display_place_id: int | None = None
    notify: NotifySettings = Field(default_factory=NotifySettings)


class FilterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field("", max_length=500)
    enabled: bool = True
    mode: Literal["live", "shadow"] = "live"
    definition: FilterDefinition = Field(default_factory=FilterDefinition)
    note: str = Field("", max_length=200)  # version note


class FilterOut(BaseModel):
    id: int
    name: str
    description: str
    enabled: bool
    mode: Literal["live", "shadow"]
    sort_order: int
    version: int
    version_id: int
    definition: FilterDefinition
    created_at: float
    updated_at: float


Group.model_rebuild()
