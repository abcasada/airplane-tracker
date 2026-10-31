from dataclasses import dataclass
from typing import Literal, Protocol

Priority = Literal["low", "normal", "high", "critical"]


@dataclass(frozen=True)
class OutgoingNotification:
    hex: str
    title: str
    message: str
    channel: str
    priority: Priority
    alarm_stream: bool = False
    image: str | None = None
    click_url: str | None = None
    military: bool = False
    alert_ids: tuple[int, ...] = ()
    eta_at: float | None = None  # epoch seconds; shown as a live countdown
    update: bool = False  # in-place refresh of a notification already showing: don't re-alert
    actions: bool = True

    @property
    def tag(self) -> str:
        # One notification per aircraft: later alerts for it replace this one in place.
        return f"aircraft-{self.hex}"


class NotifyError(Exception):
    pass


class Notifier(Protocol):
    name: str

    async def send(self, n: OutgoingNotification) -> None: ...
