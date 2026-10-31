"""Parser for readsb-style JSON ("ac" list), used by adsb.fi, adsb.lol and local receivers."""

import math
import re
from typing import Any
from urllib.parse import quote

import httpx

from app.clock import Clock

from .base import AreaQuery, Observation, PosSource, SourceBatch, SourceError

MAX_POSITION_AGE_S = 60.0
_HEX = re.compile(r"^~?[0-9a-f]{6}$")
_REG = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,11}$")


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float) and math.isfinite(v):
        return float(v)
    return None


def _str(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s or None


def _pos_source(kind: Any) -> PosSource:
    k = str(kind or "")
    if k.startswith(("adsb", "adsr", "adsc")):
        return "adsb"
    if k.startswith("mlat"):
        return "mlat"
    if k.startswith("tisb"):
        return "tisb"
    return "other"


def parse_aircraft(ac: dict[str, Any], now: float, source: str) -> Observation | None:
    hex_ = _str(ac.get("hex"))
    if not hex_:
        return None
    seen = _num(ac.get("seen")) or 0.0
    obs = Observation(hex=hex_.lower(), source=source, seen_at=now - seen)

    lat, lon = _num(ac.get("lat")), _num(ac.get("lon"))
    seen_pos = _num(ac.get("seen_pos"))
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
        age = seen_pos if seen_pos is not None else seen
        if age <= MAX_POSITION_AGE_S:
            obs.lat, obs.lon, obs.pos_time = lat, lon, now - age

    alt_baro = ac.get("alt_baro")
    if alt_baro == "ground":
        obs.on_ground = True
    else:
        obs.alt_baro_ft = _num(alt_baro)
    obs.alt_geom_ft = _num(ac.get("alt_geom"))
    obs.gs_kt = _num(ac.get("gs"))
    obs.track_deg = _num(ac.get("track"))
    if obs.track_deg is None:
        obs.track_deg = _num(ac.get("true_heading"))
    obs.baro_rate_fpm = _num(ac.get("baro_rate"))
    obs.geom_rate_fpm = _num(ac.get("geom_rate"))
    cs = _str(ac.get("flight"))
    obs.callsign = cs.upper() if cs else None
    obs.squawk = _str(ac.get("squawk"))
    em = _str(ac.get("emergency"))
    obs.emergency = em if em and em != "none" else None
    obs.category = _str(ac.get("category"))
    obs.nav_heading_deg = _num(ac.get("nav_heading"))
    obs.nav_altitude_ft = _num(ac.get("nav_altitude_mcp")) or _num(ac.get("nav_altitude_fms"))
    obs.pos_source = _pos_source(ac.get("type"))
    nac = _num(ac.get("nac_p"))
    obs.nac_p = int(nac) if nac is not None else None

    reg = _str(ac.get("r"))
    obs.registration = reg.upper() if reg else None
    t = _str(ac.get("t"))
    obs.type_code = t.upper() if t else None
    obs.type_desc = _str(ac.get("desc"))
    obs.owner_op = _str(ac.get("ownOp"))
    obs.year = _str(ac.get("year"))
    flags = _num(ac.get("dbFlags"))
    obs.db_flags = int(flags) if flags is not None else 0
    return obs


def parse_payload(payload: Any, source: str, fallback_now: float) -> SourceBatch:
    if not isinstance(payload, dict):
        raise SourceError(f"{source}: unexpected response shape")
    now_ms = _num(payload.get("now"))
    now = now_ms / 1000.0 if now_ms and now_ms > 1e11 else (now_ms or fallback_now)
    batch = SourceBatch(source=source, fetched_at=now)
    for ac in payload.get("ac") or payload.get("aircraft") or []:
        if isinstance(ac, dict) and (obs := parse_aircraft(ac, now, source)):
            batch.observations.append(obs)
    return batch


class ReadsbHttpSource:
    """An aggregator that serves readsb JSON for a point/radius query, and optionally lookups of
    specific aircraft by hex (comma-separated) or registration."""

    def __init__(
        self,
        name: str,
        url_template: str,
        http: httpx.AsyncClient,
        clock: Clock,
        max_rate_per_s: float,
        hex_url_template: str | None = None,
        reg_url_template: str | None = None,
    ) -> None:
        self.name = name
        self.url_template = url_template
        self.http = http
        self.clock = clock
        self.max_rate_per_s = max_rate_per_s
        self.hex_url_template = hex_url_template
        self.reg_url_template = reg_url_template

    def url(self, q: AreaQuery) -> str:
        return self.url_template.format(
            lat=round(q.lat, 4), lon=round(q.lon, 4), nm=max(1, min(250, math.ceil(q.radius_nm)))
        )

    async def _get(self, url: str) -> SourceBatch:
        try:
            resp = await self.http.get(url, headers={"Accept-Encoding": "gzip"}, timeout=15)
        except httpx.HTTPError as e:
            raise SourceError(f"{self.name}: {type(e).__name__}: {e}") from e
        if resp.status_code != 200:
            raise SourceError(f"{self.name}: HTTP {resp.status_code}", resp.status_code)
        try:
            payload = resp.json()
        except ValueError as e:
            raise SourceError(f"{self.name}: invalid JSON") from e
        return parse_payload(payload, self.name, self.clock.now())

    async def fetch(self, query: AreaQuery) -> SourceBatch:
        return await self._get(self.url(query))

    async def fetch_hexes(self, hexes: list[str]) -> SourceBatch:
        if self.hex_url_template is None:
            raise SourceError(f"{self.name}: no hex lookup")
        clean = [h.lower() for h in hexes if _HEX.match(h.lower())]
        if not clean:
            return SourceBatch(self.name, self.clock.now())
        return await self._get(self.hex_url_template.format(hexes=",".join(clean)))

    async def fetch_registration(self, registration: str) -> SourceBatch:
        reg = registration.strip().upper()
        if self.reg_url_template is None or not _REG.match(reg):
            raise SourceError(f"{self.name}: no registration lookup for {registration!r}")
        return await self._get(self.reg_url_template.format(reg=quote(reg, safe="")))


def adsbfi(http: httpx.AsyncClient, clock: Clock) -> ReadsbHttpSource:
    return ReadsbHttpSource(
        "adsb.fi",
        "https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{nm}",
        http,
        clock,
        max_rate_per_s=1.0,
        hex_url_template="https://opendata.adsb.fi/api/v2/hex/{hexes}",
        reg_url_template="https://opendata.adsb.fi/api/v2/registration/{reg}",
    )


def adsblol(http: httpx.AsyncClient, clock: Clock) -> ReadsbHttpSource:
    # No published limit; treated like adsb.fi.
    return ReadsbHttpSource(
        "adsb.lol",
        "https://api.adsb.lol/v2/point/{lat}/{lon}/{nm}",
        http,
        clock,
        max_rate_per_s=1.0,
        hex_url_template="https://api.adsb.lol/v2/hex/{hexes}",
        reg_url_template="https://api.adsb.lol/v2/reg/{reg}",
    )
