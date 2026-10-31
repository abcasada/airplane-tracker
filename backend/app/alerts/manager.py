"""Turns filter matches into alert episodes and notifications.

Always on: one episode per (filter, aircraft); while it keeps matching there is no new
notification. Several filters matching one aircraft in the same cycle send one combined
notification. Everything else (repeat suppression, daily caps, quiet hours) is opt-in and off
by default. The one exception is the daily safety ceiling, a guard against runaway filters.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.clock import Clock
from app.db import Database
from app.filters.engine import CompiledFilter
from app.filters.facts import LiveFacts, display_place
from app.filters.models import FilterOut, NotifySettings, QuietHours
from app.notify.base import Notifier, NotifyError, OutgoingNotification
from app.places.models import Place

from . import format as fmt

log = logging.getLogger(__name__)

PRIORITY_RANK = {"low": 0, "normal": 1, "high": 2, "critical": 3}
DB_TOUCH_INTERVAL_S = 60.0
# In-place ETA updates: only for a meaningful shift, and never chatty.
ETA_UPDATE_MIN_SHIFT_S = 60.0
ETA_UPDATE_MIN_GAP_S = 60.0
ETA_UPDATE_MAX = 3

# Display fields stored with every alert, besides what the filter itself references.
DISPLAY_FACTS = [
    ("callsign", None), ("registration", None), ("type_code", None), ("type_description", None),
    ("operator", None), ("military", None), ("military_score", None), ("altitude_ft", None),
    ("ground_speed_kt", None), ("vertical_rate_fpm", None), ("squawk", None),
    ("plane_alert_category", None),
]  # fmt: skip


class AlertingSettings(BaseModel):
    episode_end_after_s: float = Field(120.0, ge=10, le=3600)
    daily_ceiling: int = Field(400, ge=1, le=5000)
    repeat_suppress_min: float | None = Field(None, ge=0, le=10080)
    quiet_hours: QuietHours | None = None
    timezone: str = "UTC"


@dataclass
class Match:
    filter: FilterOut
    compiled: CompiledFilter
    facts: LiveFacts


@dataclass
class Episode:
    alert_id: int
    filter_id: int
    hex: str
    started_at: float
    last_matched_at: float
    closest_nm: float | None
    closest_at: float | None
    touched_at: float


@dataclass
class Sent:
    """The notification currently showing for one aircraft, for in-place ETA updates."""

    lead: tuple[int, str]  # (filter id, hex) of the episode that wrote it
    out: OutgoingNotification
    filter_names: tuple[str, ...]
    last_sent_at: float
    updates: int = 0


def in_quiet_hours(q: QuietHours, now: float, tz: ZoneInfo) -> bool:
    t = datetime.fromtimestamp(now, tz)
    minutes = t.hour * 60 + t.minute
    sh, sm = (int(x) for x in q.start.split(":"))
    eh, em = (int(x) for x in q.end.split(":"))
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        return False
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def local_day_start(now: float, tz: ZoneInfo) -> float:
    t = datetime.fromtimestamp(now, tz)
    return t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def suppression_reason(
    mode: str,
    n: NotifySettings,
    g: AlertingSettings,
    now: float,
    tz: ZoneInfo,
    last_notified_by_filter: float | None,
    last_notified_by_any: float | None,
    notified_today_by_filter: int,
) -> str | None:
    """Why a new episode should not notify, or None. Every rule here is opt-in.

    Pure, so live alerting and the backtest apply exactly the same rules."""
    if mode != "live":
        return "shadow mode"
    if not n.enabled:
        return "notifications off for this filter"
    if n.quiet_hours and in_quiet_hours(n.quiet_hours, now, tz):
        return "filter quiet hours"
    if g.quiet_hours and n.priority != "critical" and in_quiet_hours(g.quiet_hours, now, tz):
        return "quiet hours"
    if (
        n.repeat_suppress_min
        and last_notified_by_filter is not None
        and last_notified_by_filter >= now - n.repeat_suppress_min * 60
    ):
        return f"notified about this aircraft in the last {n.repeat_suppress_min:g} min"
    if (
        g.repeat_suppress_min
        and last_notified_by_any is not None
        and last_notified_by_any >= now - g.repeat_suppress_min * 60
    ):
        return f"any filter notified about it in the last {g.repeat_suppress_min:g} min"
    if n.max_per_day and notified_today_by_filter >= n.max_per_day:
        return f"daily limit of {n.max_per_day} reached"
    return None


class AlertManager:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        get_notifier: Callable[[], Awaitable[Notifier | None]],
        public_url: str | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.get_notifier = get_notifier
        self.public_url = public_url.rstrip("/") if public_url else None
        self.on_event = on_event
        self.settings = AlertingSettings()
        self.episodes: dict[tuple[int, str], Episode] = {}
        self.sent: dict[str, Sent] = {}
        self.ceiling_hit_at: float | None = None

    async def load(self) -> None:
        self.settings = AlertingSettings.model_validate(await self.db.get_json("alerting", {}))
        rows = await self.db.fetchall(
            "SELECT id, filter_id, hex, started_at, last_matched_at, closest_nm, closest_at "
            "FROM alerts WHERE state = 'active' AND filter_id IS NOT NULL"
        )
        self.episodes = {
            (r["filter_id"], r["hex"]): Episode(
                r["id"], r["filter_id"], r["hex"], r["started_at"], r["last_matched_at"],
                r["closest_nm"], r["closest_at"], r["last_matched_at"],
            )
            for r in rows
        }  # fmt: skip

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.settings.timezone)
        except (KeyError, ValueError):
            return ZoneInfo("UTC")

    async def process(
        self, now: float, matches: list[Match], places: dict[int, Place]
    ) -> list[int]:
        """Update episodes for this evaluation cycle. Returns ids of newly created alerts."""
        seen: dict[tuple[int, str], Match] = {}
        new_by_hex: dict[str, list[tuple[Match, int]]] = defaultdict(list)
        for m in matches:
            key = (m.filter.id, m.facts.hex)
            seen[key] = m
            place = display_place(m.filter.definition.display_place_id, m.facts)
            dist = fmt.distance_for(m.facts, place)
            ep = self.episodes.get(key)
            if ep is None:
                alert_id = await self._create_alert(m, place, now, dist)
                self.episodes[key] = Episode(
                    alert_id, m.filter.id, m.facts.hex, now, now, dist, now if dist else None, now
                )
                new_by_hex[m.facts.hex].append((m, alert_id))
                continue
            ep.last_matched_at = now
            if dist is not None and (ep.closest_nm is None or dist < ep.closest_nm):
                ep.closest_nm, ep.closest_at = dist, now
            if now - ep.touched_at >= DB_TOUCH_INTERVAL_S:
                await self._touch(ep)

        for key, ep in list(self.episodes.items()):
            if key not in seen and now - ep.last_matched_at >= self.settings.episode_end_after_s:
                await self.db.execute(
                    "UPDATE alerts SET state = 'ended', ended_at = ?, last_matched_at = ?, "
                    "closest_nm = ?, closest_at = ? WHERE id = ?",
                    (now, ep.last_matched_at, ep.closest_nm, ep.closest_at, ep.alert_id),
                )
                del self.episodes[key]
        for hex_, sent in list(self.sent.items()):
            if sent.lead not in self.episodes:
                del self.sent[hex_]

        created: list[int] = []
        for hex_, items in new_by_hex.items():
            created.extend(a for _, a in items)
            await self._notify(hex_, items, places, now)
        for hex_, sent in list(self.sent.items()):
            lead = seen.get(sent.lead)
            if lead is not None and hex_ not in new_by_hex:
                await self._maybe_update_eta(sent, lead, now)
        return created

    async def _maybe_update_eta(self, sent: Sent, m: Match, now: float) -> None:
        n = m.filter.definition.notify
        old = sent.out.eta_at
        if not n.update_eta or old is None or sent.updates >= ETA_UPDATE_MAX:
            return
        if now - sent.last_sent_at < ETA_UPDATE_MIN_GAP_S or old <= now:
            return
        place = display_place(m.filter.definition.display_place_id, m.facts)
        eta = fmt.eta_seconds(m.facts, place)
        if eta is None or eta <= 0:
            return
        eta_at = now + eta
        if abs(eta_at - old) < ETA_UPDATE_MIN_SHIFT_S:
            return
        if await self.sent_today(now) >= self.settings.daily_ceiling:
            self.ceiling_hit_at = now
            return
        values = fmt.template_values(m.facts, place, list(sent.filter_names))
        title, message = self._render(m, values)
        out = replace(sent.out, title=title, message=message, eta_at=eta_at, update=True)
        error = await self._send(out)
        await self._record(out, "update", now, error)
        if error is None:
            sent.out, sent.last_sent_at = out, now
            sent.updates += 1
        else:
            log.warning("notification update failed: %s", error)

    async def _touch(self, ep: Episode) -> None:
        ep.touched_at = ep.last_matched_at
        await self.db.execute(
            "UPDATE alerts SET last_matched_at = ?, closest_nm = ?, closest_at = ? WHERE id = ?",
            (ep.last_matched_at, ep.closest_nm, ep.closest_at, ep.alert_id),
        )

    async def _create_alert(
        self, m: Match, place: Place | None, now: float, dist: float | None
    ) -> int:
        t = m.facts.track
        keys = m.compiled.referenced() | set(DISPLAY_FACTS)
        if place is not None:
            keys |= {("distance_nm", place.id), ("direction_from_place", place.id),
                     ("elevation_angle_deg", place.id)}  # fmt: skip
        facts = m.facts.snapshot(keys)
        cls = t.classification
        pa = t.info.plane_alert if t.info else None
        if cls is not None:
            facts["_military_reasons"] = list(cls.reasons)
        facts["_position"] = [t.lat, t.lon] if t.has_position else None
        photo = pa.images[0] if pa and pa.images else None
        return await self.db.insert(
            "INSERT INTO alerts(filter_id, filter_version_id, filter_name, mode, hex, callsign, "
            "registration, type_code, type_desc, military, place_id, state, started_at, "
            "last_matched_at, closest_nm, closest_at, facts, photo_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
            (
                m.filter.id, m.filter.version_id, m.filter.name, m.filter.mode, t.hex, t.callsign,
                t.registration, t.type_code, t.type_desc, int(bool(cls and cls.military)),
                place.id if place else None, now, now, dist, now if dist is not None else None,
                json.dumps(facts), photo,
            ),
        )  # fmt: skip

    async def _suppression(self, m: Match, now: float) -> str | None:
        n = m.filter.definition.notify
        g = self.settings
        hex_ = m.facts.hex
        last_filter = last_any = None
        today = 0
        if n.repeat_suppress_min:
            row = await self.db.fetchone(
                "SELECT max(started_at) FROM alerts WHERE filter_id = ? AND hex = ? "
                "AND notified = 1",
                (m.filter.id, hex_),
            )
            last_filter = row[0] if row else None
        if g.repeat_suppress_min:
            row = await self.db.fetchone(
                "SELECT max(started_at) FROM alerts WHERE hex = ? AND notified = 1", (hex_,)
            )
            last_any = row[0] if row else None
        if n.max_per_day:
            row = await self.db.fetchone(
                "SELECT count(*) FROM alerts WHERE filter_id = ? AND notified = 1 "
                "AND started_at >= ?",
                (m.filter.id, local_day_start(now, self.tz)),
            )
            today = int(row[0]) if row else 0
        return suppression_reason(m.filter.mode, n, g, now, self.tz, last_filter, last_any, today)

    async def sent_today(self, now: float) -> int:
        start = now - (now % 86400)  # UTC day
        row = await self.db.fetchone(
            "SELECT count(*) FROM notifications WHERE ok = 1 AND sent_at >= ?", (start,)
        )
        return int(row[0]) if row else 0

    async def _notify(
        self, hex_: str, items: list[tuple[Match, int]], places: dict[int, Place], now: float
    ) -> None:
        sendable: list[tuple[Match, int]] = []
        for m, alert_id in items:
            reason = await self._suppression(m, now)
            if reason:
                await self.db.execute(
                    "UPDATE alerts SET suppressed_reason = ? WHERE id = ?", (reason, alert_id)
                )
            else:
                sendable.append((m, alert_id))
        for m, alert_id in items:
            self._emit(m, alert_id, now, notified=any(a == alert_id for _, a in sendable))
        if not sendable:
            return
        if await self.sent_today(now) >= self.settings.daily_ceiling:
            self.ceiling_hit_at = now
            for _, alert_id in sendable:
                await self.db.execute(
                    "UPDATE alerts SET suppressed_reason = 'daily safety limit reached' "
                    "WHERE id = ?",
                    (alert_id,),
                )
            log.warning("daily notification ceiling reached; not sending")
            return

        sendable.sort(key=lambda x: -PRIORITY_RANK[x[0].filter.definition.notify.priority])
        lead, _ = sendable[0]
        n = lead.filter.definition.notify
        place = display_place(lead.filter.definition.display_place_id, lead.facts)
        names = [m.filter.name for m, _ in sendable]
        values = fmt.template_values(lead.facts, place, names)
        title, message = self._render(lead, values)
        track = lead.facts.track
        pa = track.info.plane_alert if track.info else None
        image = pa.images[0] if n.include_photo and pa and pa.images else None
        eta = fmt.eta_seconds(lead.facts, place)
        out = OutgoingNotification(
            hex=hex_,
            title=title,
            message=message,
            channel=n.channel,
            priority=n.priority,
            alarm_stream=any(m.filter.definition.notify.alarm_stream for m, _ in sendable),
            image=image,
            click_url=f"{self.public_url}/#/aircraft/{hex_}" if self.public_url else None,
            military=bool(track.classification and track.classification.military),
            alert_ids=tuple(a for _, a in sendable),
            eta_at=now + eta if eta is not None and eta > 0 else None,
        )
        error = await self._send(out)
        await self._record(out, "new", now, error)
        for alert_id in out.alert_ids:
            await self.db.execute(
                "UPDATE alerts SET notified = ?, title = ?, message = ?, suppressed_reason = ? "
                "WHERE id = ?",
                (int(error is None), out.title, out.message,
                 f"send failed: {error}" if error else None, alert_id),
            )  # fmt: skip
        if error:
            log.warning("notification failed: %s", error)
        else:
            self.sent[hex_] = Sent((lead.filter.id, hex_), out, tuple(names), now)

    @staticmethod
    def _render(m: Match, values: dict[str, str]) -> tuple[str, str]:
        n = m.filter.definition.notify
        title = (
            fmt.render(n.title_template, values) if n.title_template else fmt.default_title(values)
        )
        message = (
            fmt.render(n.message_template, values)
            if n.message_template
            else fmt.default_message(values)
        )
        return title[:200], message[:1000]

    async def _send(self, out: OutgoingNotification) -> str | None:
        notifier = await self.get_notifier()
        if notifier is None:
            return "ntfy is not configured (Settings → Notifications)"
        try:
            await notifier.send(out)
        except NotifyError as e:
            return str(e)
        return None

    async def _record(
        self, out: OutgoingNotification, kind: str, now: float, error: str | None
    ) -> None:
        await self.db.insert(
            "INSERT INTO notifications(hex, alert_ids, channel, kind, sent_at, title, message, ok, "
            "error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (out.hex, json.dumps(list(out.alert_ids)), out.channel, kind, now, out.title,
             out.message, int(error is None), error),
        )  # fmt: skip

    def _emit(self, m: Match, alert_id: int, now: float, notified: bool) -> None:
        if self.on_event is None:
            return
        t = m.facts.track
        self.on_event(
            "alert",
            {
                "id": alert_id,
                "filter_id": m.filter.id,
                "filter_name": m.filter.name,
                "mode": m.filter.mode,
                "hex": t.hex,
                "callsign": t.callsign,
                "type_code": t.type_code,
                "type_desc": t.type_desc,
                "started_at": now,
                "notified": notified,
            },
        )
