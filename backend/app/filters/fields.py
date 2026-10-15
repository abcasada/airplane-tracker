"""The filter field catalog. Every fact a filter can test is defined here, with help text.

Adding a capability means adding a field: the builder UI, live preview, funnel, alert
explanations and backtests all pick it up from this catalog.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from app import geo
from app.places.models import Place

from .facts import LiveFacts

FieldType = Literal["number", "string", "bool", "enum", "list"]

OPS_BY_TYPE: dict[FieldType, list[str]] = {
    "number": ["lt", "le", "gt", "ge", "between", "eq", "ne", "exists", "not_exists"],
    "string": ["in", "not_in", "glob", "not_glob", "contains", "regex", "exists", "not_exists"],
    "bool": ["is_true", "is_false"],
    "enum": ["in", "not_in", "exists", "not_exists"],
    "list": ["contains_any", "contains_all", "contains_none", "exists", "not_exists"],
}

EMERGENCY_SQUAWKS = {"7500": "hijack", "7600": "radio failure", "7700": "general emergency"}

CATEGORY_LABELS = {
    "A1": "Light (< 15,500 lb)", "A2": "Small (15,500–75,000 lb)",
    "A3": "Large (75,000–300,000 lb)", "A4": "High-vortex large (e.g. B757)",
    "A5": "Heavy (> 300,000 lb)", "A6": "High performance", "A7": "Rotorcraft",
    "B1": "Glider", "B2": "Lighter than air", "B3": "Parachutist", "B4": "Ultralight",
    "B6": "Drone (UAV)", "B7": "Space vehicle", "C1": "Emergency vehicle (surface)",
    "C2": "Service vehicle (surface)", "C3": "Obstacle",
}  # fmt: skip

SIZE_CLASSES = ["large", "medium", "light"]
_SIZE_BY_CATEGORY = {
    "A3": "large", "A4": "large", "A5": "large", "A2": "medium", "A6": "medium",
    "A1": "light", "A7": "light", "B1": "light", "B2": "medium", "B4": "light", "B6": "light",
}  # fmt: skip
COMPASS8 = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

Compute = Callable[[LiveFacts, Place | None], Any]


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str
    group: str
    type: FieldType
    help: str
    compute: Compute
    unit: str | None = None
    place: bool = False
    place_roles: tuple[str, ...] = ()
    enum: tuple[tuple[str, str], ...] = ()  # (value, label)
    suggest: str | None = None  # autocomplete source for the builder

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "group": self.group,
            "type": self.type,
            "unit": self.unit,
            "help": self.help,
            "place": self.place,
            "place_roles": list(self.place_roles),
            "ops": OPS_BY_TYPE[self.type],
            "enum": [{"value": v, "label": lbl} for v, lbl in self.enum],
            "suggest": self.suggest,
        }


FIELDS: dict[str, FieldDef] = {}


def _add(fd: FieldDef) -> None:
    assert fd.key not in FIELDS, fd.key
    FIELDS[fd.key] = fd


# ---- helpers ------------------------------------------------------------------------------------


def _t(f: LiveFacts) -> Any:
    return f.track


def _cls(f: LiveFacts) -> Any:
    return f.track.classification


def _pa(f: LiveFacts) -> Any:
    return f.track.info.plane_alert if f.track.info else None


def size_class(f: LiveFacts) -> str:
    cat = f.track.category
    if cat in _SIZE_BY_CATEGORY:
        return _SIZE_BY_CATEGORY[cat]
    return "medium"


def _distance(f: LiveFacts, p: Place | None) -> float | None:
    t = f.track
    if p is None or t.lat is None or t.lon is None:
        return None
    if p.role == "area" and p.polygon:
        if geo.point_in_polygon(t.lat, t.lon, p.polygon):
            return 0.0
        return _distance_to_polygon(t.lat, t.lon, p)
    return geo.haversine_nm(p.lat, p.lon, t.lat, t.lon)


def _distance_to_polygon(lat: float, lon: float, p: Place) -> float:
    assert p.polygon
    px, py = geo.to_local(lat, lon, p.lat, p.lon)
    pts = [geo.to_local(a, b, p.lat, p.lon) for a, b in p.polygon]
    best = math.inf
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1], strict=True):
        dx, dy = x2 - x1, y2 - y1
        seg = dx * dx + dy * dy
        u = 0.0 if seg == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg))
        best = min(best, math.hypot(px - (x1 + u * dx), py - (y1 + u * dy)))
    return best


def _bearing_from_place(f: LiveFacts, p: Place | None) -> float | None:
    t = f.track
    if p is None or t.lat is None or t.lon is None:
        return None
    return geo.bearing_deg(p.lat, p.lon, t.lat, t.lon)


def _height_above(f: LiveFacts, p: Place | None) -> float | None:
    alt = f.track.altitude_ft
    if p is None:
        return None
    if f.track.on_ground:
        return 0.0
    return None if alt is None else alt - p.elevation_ft


def _elevation_angle(f: LiveFacts, p: Place | None) -> float | None:
    d, h = f.get("distance_nm", p.id if p else None), _height_above(f, p)
    if d is None or h is None:
        return None
    return geo.elevation_angle_deg(d, h)


def _slant(f: LiveFacts, p: Place | None) -> float | None:
    d, h = f.get("distance_nm", p.id if p else None), _height_above(f, p)
    if d is None or h is None:
        return None
    return geo.slant_range_nm(d, max(h, 0.0))


def _visible_now(f: LiveFacts, p: Place | None) -> bool | None:
    if p is None or p.visibility is None:
        return None
    if f.track.on_ground:
        return False
    elev, slant = _elevation_angle(f, p), _slant(f, p)
    if elev is None or slant is None:
        return None
    v = p.visibility
    limit = {
        "large": v.max_slant_nm_large,
        "medium": v.max_slant_nm_medium,
        "light": v.max_slant_nm_light,
    }[size_class(f)]
    return elev >= v.min_elevation_deg and slant <= limit


def _inside_area(f: LiveFacts, p: Place | None) -> bool | None:
    t = f.track
    if p is None or t.lat is None or t.lon is None:
        return None
    if p.polygon:
        inside = geo.point_in_polygon(t.lat, t.lon, p.polygon)
    else:
        inside = geo.haversine_nm(p.lat, p.lon, t.lat, t.lon) <= p.radius_nm
    alt = t.altitude_ft if not t.on_ground else p.elevation_ft
    if inside and alt is not None:
        if p.alt_floor_ft is not None and alt < p.alt_floor_ft:
            return False
        if p.alt_ceiling_ft is not None and alt > p.alt_ceiling_ft:
            return False
    return inside


def _angle_to_place(f: LiveFacts, p: Place | None) -> float | None:
    t = f.track
    if p is None or t.lat is None or t.lon is None or t.track_deg is None:
        return None
    to_place = geo.bearing_deg(t.lat, t.lon, p.lat, p.lon)
    return abs(geo.angle_diff(t.track_deg, to_place))


def _local_dt(f: LiveFacts) -> datetime:
    return datetime.fromtimestamp(f.ctx.now, f.ctx.tz)


_COMPANY_SUFFIXES = re.compile(r"\b(INC|CO|CORP|CORPORATION|LLC|LTD|LIMITED|COMPANY|NA)\b")


def _norm_name(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", _COMPANY_SUFFIXES.sub("", name.upper()))


def _operator(f: LiveFacts) -> str | None:
    t = f.track
    pa = _pa(f)
    candidates = [t.airline.name if t.airline else None, t.owner_op, pa.operator if pa else None]
    parts: list[str] = []
    seen: list[str] = []
    for name in candidates:
        if not name:
            continue
        n = _norm_name(name)
        # "Southwest Airlines" and "SOUTHWEST AIRLINES CO" are the same operator.
        if any(n.startswith(s) or s.startswith(n) for s in seen):
            continue
        seen.append(n)
        parts.append(name)
    return " / ".join(parts) or None


def _confirmed_squawk(f: LiveFacts) -> str | None:
    t = f.track
    return t.squawk if t.squawk and t.squawk_streak >= 2 else None


def _year(f: LiveFacts) -> float | None:
    y = f.track.year
    try:
        return float(y) if y else None
    except ValueError:
        return None


def _flag(attr: str) -> Compute:
    def compute(f: LiveFacts, _p: Place | None) -> bool | None:
        info = f.track.info
        return bool(getattr(info, attr)) if info else None

    return compute


# ---- catalog ------------------------------------------------------------------------------------

ID, AC, CL, ST, PL, TM = "Identity", "Aircraft", "Classification", "State", "Place", "Time"

for fd in [
    FieldDef("hex", "ICAO hex address", ID, "string",
             "The transponder's 24-bit address, e.g. ae1234.", lambda f, _: f.track.hex),
    FieldDef("registration", "Registration", ID, "string",
             "Tail number or military serial, e.g. N12345 or 98-0001.",
             lambda f, _: f.track.registration),
    FieldDef("callsign", "Callsign", ID, "string",
             "Flight callsign as broadcast, e.g. UAL1816 or RCH452.",
             lambda f, _: f.track.callsign, suggest="callsign"),
    FieldDef("airline_code", "Airline code", ID, "string",
             "ICAO airline code from the callsign (UAL, SWA…), when the callsign is an airline's.",
             lambda f, _: f.track.airline.icao if f.track.airline else None, suggest="airline"),
    FieldDef("operator", "Operator / owner", ID, "string",
             "Airline name, registered owner and plane-alert-db operator combined. Use "
             "'contains' to match part of a name.",
             lambda f, _: _operator(f), suggest="operator"),
    FieldDef("type_code", "Aircraft type", AC, "string",
             "ICAO type designator, e.g. C17, K35R, B752. Glob patterns like B7* work.",
             lambda f, _: f.track.type_code, suggest="type_code"),
    FieldDef("type_description", "Type description", AC, "string",
             "Manufacturer and model, e.g. 'BOEING C-17 Globemaster 3'.",
             lambda f, _: f.track.type_desc),
    FieldDef("category", "Emitter category", AC, "enum",
             "Size/kind the aircraft broadcasts about itself.",
             lambda f, _: f.track.category, enum=tuple(CATEGORY_LABELS.items())),
    FieldDef("size_class", "Size class", AC, "enum",
             "Large, medium or light, from the emitter category. Used for visibility distances.",
             lambda f, _: size_class(f), enum=tuple((s, s.title()) for s in SIZE_CLASSES)),
    FieldDef("year_built", "Year built", AC, "number", "Year of manufacture, when known.",
             lambda f, _: _year(f)),
    FieldDef("military", "Military", CL, "bool",
             "Classified military: the combined military score reaches your threshold "
             "(Military settings), or you marked it.",
             lambda f, _: _cls(f).military if _cls(f) else None),
    FieldDef("military_score", "Military score", CL, "number",
             "0–1 confidence that it's military, combining all military signals.",
             lambda f, _: _cls(f).military_score if _cls(f) else None),
    FieldDef("military_signals", "Military signals", CL, "list",
             "Which military clues fired: db_flag, plane_alert_mil, mil_address, mil_type, "
             "dual_use_type, owner_keyword, callsign_prefix, tactical_callsign, "
             "registration_pattern.",
             lambda f, _: sorted(_cls(f).signals) if _cls(f) else None,
             enum=tuple((k, k.replace("_", " ")) for k in (
                 "db_flag", "plane_alert_mil", "mil_address", "mil_type", "dual_use_type",
                 "owner_keyword", "callsign_prefix", "tactical_callsign", "registration_pattern"))),
    FieldDef("government", "Government", CL, "bool",
             "Government (non-military) aircraft, e.g. FAA, CBP, state aircraft.",
             lambda f, _: _cls(f).government if _cls(f) else None),
    FieldDef("police", "Police", CL, "bool", "Police and law-enforcement aircraft.",
             lambda f, _: _cls(f).police if _cls(f) else None),
    FieldDef("plane_alert_listed", "On plane-alert-db", CL, "bool",
             "Listed in plane-alert-db, the community list of ~17,000 interesting aircraft.",
             lambda f, _: _pa(f) is not None),
    FieldDef("plane_alert_cmpg", "plane-alert-db class", CL, "enum",
             "plane-alert-db's Civ / Mil / Pol / Gov classification.",
             lambda f, _: _pa(f).cmpg if _pa(f) else None,
             enum=(("Civ", "Civil"), ("Mil", "Military"), ("Pol", "Police"),
                   ("Gov", "Government"))),
    FieldDef("plane_alert_category", "plane-alert-db category", CL, "string",
             "e.g. USAF, Zoomies (fast jets), Oxcart (intelligence), Gunship, Dictator Alert.",
             lambda f, _: _pa(f).category if _pa(f) else None, suggest="plane_alert_category"),
    FieldDef("plane_alert_tags", "plane-alert-db tags", CL, "list",
             "Free-text tags from plane-alert-db, e.g. Tanker, Special Air Mission.",
             lambda f, _: list(_pa(f).tags) if _pa(f) else None, suggest="plane_alert_tag"),
    FieldDef("interesting_flag", "Flagged interesting", CL, "bool",
             "Marked 'interesting' in the tar1090 aircraft database.",
             _flag("db_interesting")),
    FieldDef("ladd", "LADD (limited display)", CL, "bool",
             "On the FAA's Limiting Aircraft Data Displayed list.", _flag("db_ladd")),
    FieldDef("pia", "PIA (privacy address)", CL, "bool",
             "Uses an FAA Privacy ICAO Address.", _flag("db_pia")),
    FieldDef("altitude_ft", "Altitude (MSL)", ST, "number",
             "Barometric altitude above mean sea level (GPS altitude if that's all there is).",
             lambda f, _: None if f.track.on_ground else f.track.altitude_ft, unit="ft"),
    FieldDef("ground_speed_kt", "Ground speed", ST, "number", "Speed over the ground.",
             lambda f, _: f.track.gs_kt, unit="kt"),
    FieldDef("vertical_rate_fpm", "Vertical rate", ST, "number",
             "Climb (+) or descent (−) rate.", lambda f, _: f.track.vertical_rate_fpm,
             unit="ft/min"),
    FieldDef("track_deg", "Track", ST, "number", "Direction of travel, degrees true.",
             lambda f, _: f.track.track_deg, unit="°"),
    FieldDef("squawk", "Squawk", ST, "string", "Current transponder code.",
             lambda f, _: f.track.squawk),
    FieldDef("emergency_squawk", "Emergency squawk", ST, "bool",
             "Squawking 7500, 7600 or 7700, seen on at least 2 consecutive updates "
             "(single-update glitches are common).",
             lambda f, _: _confirmed_squawk(f) in EMERGENCY_SQUAWKS),
    FieldDef("emergency_status", "ADS-B emergency status", ST, "string",
             "Emergency state the aircraft reports: general, lifeguard, minfuel, nordo, "
             "unlawful, downed.", lambda f, _: f.track.emergency),
    FieldDef("on_ground", "On the ground", ST, "bool", "Reporting that it's on the ground.",
             lambda f, _: f.track.on_ground),
    FieldDef("position_source", "Position source", ST, "enum",
             "How the position was obtained. MLAT and TIS-B are less precise than ADS-B.",
             lambda f, _: f.track.pos_source if f.track.has_position else None,
             enum=(("adsb", "ADS-B"), ("mlat", "MLAT"), ("tisb", "TIS-B"), ("other", "Other"))),
    FieldDef("data_age_s", "Data age", ST, "number", "Seconds since the last position.",
             lambda f, _: f.ctx.now - f.track.pos_time if f.track.pos_time else None, unit="s"),
    FieldDef("seconds_tracked", "Time tracked", ST, "number",
             "How long this aircraft has been in range.",
             lambda f, _: f.ctx.now - f.track.first_seen, unit="s"),
    FieldDef("distance_nm", "Distance", PL, "number",
             "Ground distance from the place. For areas: 0 inside, otherwise to the nearest edge.",
             _distance, unit="nm", place=True),
    FieldDef("direction_from_place", "Direction from place", PL, "enum",
             "Compass direction of the aircraft as seen from the place.",
             lambda f, p: (COMPASS8[round((b % 360) / 45) % 8]
                           if (b := _bearing_from_place(f, p)) is not None else None),
             place=True, enum=tuple((c, c) for c in COMPASS8)),
    FieldDef("bearing_from_place_deg", "Bearing from place", PL, "number",
             "Exact bearing from the place to the aircraft.", _bearing_from_place, unit="°",
             place=True),
    FieldDef("height_above_place_ft", "Height above place", PL, "number",
             "Altitude minus the place's ground elevation.", _height_above, unit="ft",
             place=True),
    FieldDef("elevation_angle_deg", "Elevation angle", PL, "number",
             "How high above the horizon it appears from the place (90° = overhead).",
             _elevation_angle, unit="°", place=True, place_roles=("viewpoint",)),
    FieldDef("slant_range_nm", "Line-of-sight distance", PL, "number",
             "Straight-line distance from the place, including height.", _slant, unit="nm",
             place=True, place_roles=("viewpoint",)),
    FieldDef("visible_now", "Visible now", PL, "bool",
             "Above the viewpoint's minimum elevation angle and within its distance limit for "
             "this size of aircraft (Places → visibility settings).",
             _visible_now, place=True, place_roles=("viewpoint",)),
    FieldDef("inside_area", "Inside area", PL, "bool",
             "Inside the area's polygon (or radius) and its altitude band, if set.",
             _inside_area, place=True, place_roles=("area",)),
    FieldDef("angle_to_place_deg", "Heading off the place", PL, "number",
             "0° = flying straight at the place, 180° = straight away from it.",
             _angle_to_place, unit="°", place=True),
    FieldDef("local_hour", "Time of day", TM, "number",
             "Local time as hours, e.g. 6.5 = 06:30. Use 'between' for a window.",
             lambda f, _: (d := _local_dt(f)).hour + d.minute / 60, unit="h"),
    FieldDef("day_of_week", "Day of week", TM, "enum", "Local day of the week.",
             lambda f, _: WEEKDAYS[_local_dt(f).weekday()],
             enum=tuple((d, d) for d in WEEKDAYS)),
]:  # fmt: skip
    _add(fd)


def catalog() -> list[dict[str, Any]]:
    return [fd.describe() for fd in FIELDS.values()]
