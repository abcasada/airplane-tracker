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
from app.classify.size import SIZE_CLASSES, size_for
from app.enrich.routes import RouteFit, fit_route
from app.history import day_of
from app.places.models import Place
from app.predict.arrival import ArrivalInputs, arrival_score
from app.predict.engine import is_active, prediction_for
from app.sun import CIVIL_DARK_DEG, SUNRISE_DEG, sun_elevation_deg

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
    # How "any active viewpoint" combines per-viewpoint values; None = not supported.
    aggregate: Literal["any", "min", "max"] | None = None

    def __post_init__(self) -> None:
        if self.place and self.aggregate is None and self.type in ("bool", "number"):
            object.__setattr__(self, "aggregate", "any" if self.type == "bool" else "min")

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
            "any_viewpoint": self.aggregate is not None
            and (not self.place_roles or "viewpoint" in self.place_roles),
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
    return size_for(f.track.category)


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


def _route_fit(f: LiveFacts) -> RouteFit | None:
    if "__route_fit" not in f._memo:
        route = f.track.route
        f._memo["__route_fit"] = fit_route(route, f.track.lat, f.track.lon) if route else None
    fit: RouteFit | None = f._memo["__route_fit"]
    return fit


def _route_value(attr: str) -> Compute:
    def compute(f: LiveFacts, _p: Place | None) -> Any:
        fit = _route_fit(f)
        return getattr(fit, attr) if fit else None

    return compute


def _airport_code(p: Place) -> str:
    return (p.airport_icao or "").upper()


def _destined_for(f: LiveFacts, p: Place | None) -> bool | None:
    fit = _route_fit(f)
    if p is None or fit is None:
        return None
    return fit.plausible and fit.destination == _airport_code(p)


def _departed_from(f: LiveFacts, p: Place | None) -> bool | None:
    fit = _route_fit(f)
    if p is None or fit is None:
        return None
    return fit.plausible and fit.origin == _airport_code(p)


def _arrival(f: LiveFacts, p: Place | None) -> tuple[float, str | None] | None:
    t = f.track
    if p is None or t.lat is None or t.lon is None:
        return None
    key = f"__arrival@{p.id}"
    if key not in f._memo:
        height = _height_above(f, p)
        runways = f.ctx.extras.get("runways", {}).get(p.id, [])
        if height is None:
            f._memo[key] = (0.0, None)
        else:
            inputs = ArrivalInputs(
                t.lat, t.lon, height, t.gs_kt, t.track_deg, t.vertical_rate_fpm, t.on_ground
            )
            f._memo[key] = arrival_score(inputs, p.lat, p.lon, runways)
    result: tuple[float, str | None] = f._memo[key]
    return result


def _pred(attr: str, kind: str) -> Compute:
    def compute(f: LiveFacts, p: Place | None) -> Any:
        pred = prediction_for(f.track, p.id) if p else None
        return getattr(pred, attr) if pred is not None and pred.kind == kind else None

    return compute


def _pred_active(f: LiveFacts, p: Place | None) -> bool | None:
    if p is None or not f.track.has_position:
        return None
    return is_active(f.track, p.id)


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


def _sun(f: LiveFacts, p: Place | None) -> float | None:
    if p is None:
        return None
    memo: dict[int, float] = f.ctx.extras.setdefault("_sun", {})
    if p.id not in memo:
        memo[p.id] = sun_elevation_deg(f.ctx.now, p.lat, p.lon)
    return memo[p.id]


def _history(method: str) -> Compute:
    def compute(f: LiveFacts, p: Place | None) -> Any:
        book = f.ctx.extras.get("sightings")
        if book is None or p is None:
            return None
        day = day_of(f.ctx.now, f.ctx.tz)
        if method == "type_visits_before":
            return book.type_visits_before(f.track.type_code, p.id, day)
        return getattr(book, method)(f.hex, p.id, day)

    return compute


# ---- catalog ------------------------------------------------------------------------------------

ID, AC, CL, ST, PL, PR, RT, HI, TM = (
    "Identity", "Aircraft", "Classification", "State", "Place", "Prediction", "Route", "History",
    "Time",
)  # fmt: skip

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
             _elevation_angle, unit="°", place=True, place_roles=("viewpoint",),
             aggregate="max"),
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
    FieldDef("schedule_active", "Within the place's active hours", PL, "bool",
             "True during the place's weekly active hours (set on the place, e.g. a range's "
             "published hours). Unknown when the place has no active hours set.",
             lambda f, p: p.schedule.is_active(f.ctx.now) if p and p.schedule else None,
             place=True),
    FieldDef("arrival_score", "Arrival score", PL, "number",
             "0–1: how much it looks like it's arriving at this airport: heading for it or lined "
             "up with a runway, descending, a believable height for the distance, slowing down. "
             "Works for any aircraft, with or without a known route.",
             lambda f, p: (a[0] if (a := _arrival(f, p)) else None), place=True,
             place_roles=("airport",)),
    FieldDef("arrival_runway", "Lined up with runway", PL, "string",
             "The runway it's lined up for, within 15 nm of the threshold (e.g. 35).",
             lambda f, p: (a[1] if (a := _arrival(f, p)) else None), place=True,
             place_roles=("airport",)),
    FieldDef("destined_for", "Claimed destination is", PL, "bool",
             "The route database says it's flying to this airport, and its position fits that "
             "route. Only airline-style callsigns have routes; military and most private flights "
             "don't.", _destined_for, place=True, place_roles=("airport",)),
    FieldDef("departed_from", "Claimed origin is", PL, "bool",
             "The route database says it came from this airport, and its position fits.",
             _departed_from, place=True, place_roles=("airport",)),
    FieldDef("predicted_visible", "Visible now or soon", PR, "bool",
             "Visible from the viewpoint now, or predicted to be within the lead time with enough "
             "confidence (Settings → Prediction, or the place's own overrides). Turns on after 2 "
             "consecutive confident predictions and off after 3 unconfident ones.",
             _pred_active, place=True, place_roles=("viewpoint",)),
    FieldDef("visible_eta_s", "Visible in", PR, "number",
             "Predicted seconds until it becomes visible (0 = visible now).",
             _pred("eta_s", "visible"), unit="s", place=True, place_roles=("viewpoint",)),
    FieldDef("visible_confidence", "Visibility confidence", PR, "number",
             "0–1: share of simulated paths that become visible, reduced for poor data and far-off "
             "ETAs.", _pred("confidence", "visible"), place=True, place_roles=("viewpoint",),
             aggregate="max"),
    FieldDef("predicted_max_elevation_deg", "Highest predicted elevation", PR, "number",
             "How high above the horizon it's expected to get from the viewpoint.",
             _pred("max_elevation_deg", "visible"), unit="°", place=True,
             place_roles=("viewpoint",), aggregate="max"),
    FieldDef("predicted_closest_nm", "Predicted closest approach", PR, "number",
             "Expected closest ground distance to the place within the prediction horizon.",
             lambda f, p: (pr.cpa_nm if (pr := prediction_for(f.track, p.id) if p else None)
                           else None), unit="nm", place=True),
    FieldDef("predicted_entry", "Inside or entering soon", PR, "bool",
             "Inside the area now, or predicted to enter within the lead time with enough "
             "confidence.", _pred_active, place=True, place_roles=("area",)),
    FieldDef("entry_eta_s", "Enters area in", PR, "number",
             "Predicted seconds until it enters the area (0 = inside now).",
             _pred("eta_s", "entry"), unit="s", place=True, place_roles=("area",)),
    FieldDef("entry_confidence", "Area entry confidence", PR, "number",
             "0–1 confidence that it enters the area.", _pred("confidence", "entry"),
             place=True, place_roles=("area",), aggregate="max"),
    FieldDef("predicted_arrival", "Arriving soon", PR, "bool",
             "Arrival score high enough (with data quality) and expected at the airport within "
             "the lead time.", _pred_active, place=True, place_roles=("airport",)),
    FieldDef("arrival_eta_s", "Arrives in", PR, "number",
             "Rough seconds until it reaches the airport, allowing for maneuvering onto final.",
             _pred("eta_s", "arrival"), unit="s", place=True, place_roles=("airport",)),
    FieldDef("route_known", "Route known", RT, "bool",
             "A route database (adsb.im) knows this callsign's route.",
             lambda f, _: f.track.route.known if f.track.route else None),
    FieldDef("route_origin", "Claimed origin", RT, "string",
             "ICAO code of the leg's departure airport, e.g. KDEN.", _route_value("origin")),
    FieldDef("route_destination", "Claimed destination", RT, "string",
             "ICAO code of the leg's arrival airport, e.g. KSLN.", _route_value("destination")),
    FieldDef("route_plausible", "Route plausible", RT, "bool",
             "The aircraft is actually near the claimed route. Callsign routes are often stale.",
             _route_value("plausible")),
    FieldDef("route_off_track_nm", "Distance off route", RT, "number",
             "How far the aircraft is from the claimed route's leg.", _route_value("off_track_nm"),
             unit="nm"),
    FieldDef("local_hour", "Time of day", TM, "number",
             "Local time as hours, e.g. 6.5 = 06:30. Use 'between' for a window.",
             lambda f, _: (d := _local_dt(f)).hour + d.minute / 60, unit="h"),
    FieldDef("day_of_week", "Day of week", TM, "enum", "Local day of the week.",
             lambda f, _: WEEKDAYS[_local_dt(f).weekday()],
             enum=tuple((d, d) for d in WEEKDAYS)),
    FieldDef("sun_elevation_deg", "Sun elevation", TM, "number",
             "How high the sun is above the place's horizon; negative below it. About −0.8° "
             "at sunrise/sunset, −6° at the end of civil twilight.",
             _sun, unit="°", place=True, aggregate="max"),
    FieldDef("daylight", "Daylight", TM, "bool",
             "The sun is up at the place (between sunrise and sunset).",
             lambda f, p: None if (e := _sun(f, p)) is None else e > SUNRISE_DEG, place=True),
    FieldDef("dark", "Dark", TM, "bool",
             "After civil twilight at the place: dark enough that aircraft lights stand out "
             "and the airframe is hard to see.",
             lambda f, p: None if (e := _sun(f, p)) is None else e < CIVIL_DARK_DEG, place=True),
    FieldDef("visits_prev_30d", "Visits in the last 30 days", HI, "number",
             "Days in the 30 days before today that this aircraft came within the place's watch "
             "radius. 0 = not seen here for a month. History starts when the app does.",
             _history("visits_before"), unit="days", place=True),
    FieldDef("days_since_visit", "Days since last visit", HI, "number",
             "Days since it was last within the place's watch radius, before today. Empty if "
             "it has never been seen there (see 'First visit'). History is kept 180 days.",
             _history("days_since_visit"), unit="days", place=True),
    FieldDef("first_visit", "First visit", HI, "bool",
             "Never seen within the place's watch radius before today, as far back as the "
             "history goes (180 days, or since the app started).",
             _history("first_visit"), place=True),
    FieldDef("type_visits_prev_30d", "Type's visits in the last 30 days", HI, "number",
             "Days in the 30 days before today that any aircraft of this type came within the "
             "place's watch radius. Low numbers mean an unusual type here.",
             _history("type_visits_before"), unit="days", place=True),
]:  # fmt: skip
    _add(fd)


def catalog() -> list[dict[str, Any]]:
    return [fd.describe() for fd in FIELDS.values()]
