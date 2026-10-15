"""Notification text. Templates use {placeholders}; no attribute access or code, by design."""

import re

from app.filters.facts import LiveFacts
from app.places.models import Place

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")

TEMPLATE_KEYS = {
    "aircraft": "Type description, or type code",
    "type_code": "ICAO type code",
    "registration": "Registration",
    "callsign": "Callsign",
    "operator": "Operator / owner",
    "hex": "ICAO hex",
    "place": "Display place name",
    "distance": "Distance from the display place",
    "direction": "Direction from the display place",
    "altitude": "Altitude",
    "speed": "Ground speed",
    "vertical": "climbing / descending / level",
    "elevation": "Elevation angle from the display place",
    "military": "'military' when classified military",
    "military_reasons": "Why it was classified military",
    "category": "plane-alert-db category",
    "tags": "plane-alert-db tags",
    "squawk": "Squawk code",
    "filters": "Names of the matching filters",
}

DEFAULT_TITLE = "✈ {aircraft} · {distance} {direction} of {place}"


def fmt_alt(ft: float | None, on_ground: bool = False) -> str:
    if on_ground:
        return "on the ground"
    if ft is None:
        return "altitude unknown"
    if ft >= 18000:
        return f"FL{round(ft / 100)}"
    return f"{round(ft):,} ft"


def _vertical(rate: float | None) -> str:
    if rate is None:
        return ""
    if rate > 300:
        return "climbing"
    if rate < -300:
        return "descending"
    return "level"


def template_values(
    facts: LiveFacts, place: Place | None, filter_names: list[str]
) -> dict[str, str]:
    t = facts.track
    cls = t.classification
    pa = t.info.plane_alert if t.info else None
    pid = place.id if place else None
    dist = facts.get("distance_nm", pid) if place else None
    elev = facts.get("elevation_angle_deg", pid) if place and place.role == "viewpoint" else None
    bearing = facts.get("bearing_from_place_deg", pid) if place else None
    return {
        "aircraft": t.type_desc or t.type_code or "Unknown aircraft",
        "type_code": t.type_code or "",
        "registration": t.registration or "",
        "callsign": t.callsign or "",
        "operator": facts.get("operator") or "",
        "hex": t.hex,
        "place": place.name if place else "",
        "distance": f"{dist:.1f} nm" if dist is not None else "",
        "direction": _compass16(bearing) if bearing is not None else "",
        "altitude": fmt_alt(t.altitude_ft, t.on_ground),
        "speed": f"{round(t.gs_kt)} kt" if t.gs_kt is not None else "",
        "vertical": _vertical(t.vertical_rate_fpm),
        "elevation": f"{round(elev)}° up" if elev is not None and elev > 0 else "",
        "military": "military" if cls and cls.military else "",
        "military_reasons": "; ".join(cls.reasons) if cls and cls.military else "",
        "category": (pa.category or "") if pa else "",
        "tags": ", ".join(pa.tags) if pa else "",
        "squawk": t.squawk or "",
        "filters": ", ".join(filter_names),
    }


_COMPASS16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]  # fmt: skip


def _compass16(deg: float) -> str:
    return _COMPASS16[round((deg % 360) / 22.5) % 16]


def render(template: str, values: dict[str, str]) -> str:
    out = _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)
    # Tidy separators left dangling by empty values.
    out = re.sub(r"(\s*·\s*){2,}", " · ", out)
    out = re.sub(r"^\s*·\s*|\s*·\s*$", "", out, flags=re.MULTILINE)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def default_title(values: dict[str, str]) -> str:
    if values["place"] and values["distance"]:
        return render(DEFAULT_TITLE, values)
    return render("✈ {aircraft}", values)


def default_message(values: dict[str, str]) -> str:
    lines = [
        render("{callsign} · {operator} · {registration}", values),
        render("{altitude} · {speed} · {vertical} · {elevation}", values),
    ]
    if values["squawk"] in ("7500", "7600", "7700"):
        lines.insert(0, f"Squawking {values['squawk']}")
    if values["military_reasons"]:
        lines.append(f"Military: {values['military_reasons']}")
    if values["category"]:
        lines.append(render("plane-alert: {category} · {tags}", values))
    lines.append(f"Filter: {values['filters']}")
    return "\n".join(line for line in lines if line)


def distance_for(facts: LiveFacts, place: Place | None) -> float | None:
    if place is None:
        return None
    d = facts.get("distance_nm", place.id)
    return float(d) if d is not None else None
