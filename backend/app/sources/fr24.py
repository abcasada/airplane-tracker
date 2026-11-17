"""Flightradar24 API (paid, Explorer plan): live positions of specific aircraft, by registration
or callsign. Never polled: `ingest/fr24.py` calls it only for on-demand lookups and for filter
candidates the free feeds lost, behind a monthly credit cap.

The API has no hex filter, so lookups go by registration or callsign (at most 15 of each per
request). Endpoint, headers and response fields follow FR24's OpenAPI spec and official SDK
(github.com/Flightradar24/fr24api-sdk-python, MIT). Per FR24's terms the data is for the
subscriber's own use: it is cached privately and never recorded, exported or committed.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import httpx

from app import USER_AGENT
from app.clock import Clock

from .base import Observation, PosSource, SourceBatch, SourceError

BASE_URL = "https://fr24api.flightradar24.com/api"
POSITIONS_URL = f"{BASE_URL}/live/flight-positions/full"
USAGE_URL = f"{BASE_URL}/usage"
MAX_PER_PARAM = 15  # registrations / callsigns per request (API limit)
ATTRIBUTION = "Flightradar24"
ATTRIBUTION_URL = "https://www.flightradar24.com"
# FR24 marks positions it extrapolated without a receiver as ESTIMATED; those aren't
# measurements, so they're shown on demand but never fed into tracks.
POS_SOURCES: dict[str, PosSource] = {"ADSB": "adsb", "MLAT": "mlat"}
_REG = re.compile(r"^[A-Z0-9-]{2,12}$")
_CALLSIGN = re.compile(r"^[A-Z0-9-]{3,8}$")
_HEX = re.compile(r"^[0-9a-f]{6}$")
_SQUAWK = re.compile(r"^[0-7]{4}$")


def norm_reg(reg: str) -> str:
    """For matching registrations across sources: uppercase, no hyphens."""
    return reg.strip().upper().replace("-", "")


def valid_registration(reg: str) -> bool:
    return bool(_REG.match(reg.strip().upper()))


def valid_callsign(cs: str) -> bool:
    return bool(_CALLSIGN.match(cs.strip().upper()))


@dataclass(slots=True)
class FR24Flight:
    """One flight from the full live-positions endpoint, in internal units (ft, kt, ft/min)."""

    fr24_id: str
    hex: str | None
    callsign: str | None
    registration: str | None
    type_code: str | None
    flight: str | None
    lat: float
    lon: float
    alt_ft: float
    gs_kt: float | None
    track_deg: float | None
    vrate_fpm: float | None
    squawk: str | None
    pos_time: float
    data_source: str  # FR24's own label: ADSB, MLAT, ESTIMATED, …
    painted_as: str | None = None
    operating_as: str | None = None
    orig_icao: str | None = None
    orig_iata: str | None = None
    dest_icao: str | None = None
    dest_iata: str | None = None
    eta: float | None = None

    @property
    def estimated(self) -> bool:
        return self.data_source not in POS_SOURCES

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "estimated": self.estimated}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FR24Flight:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_observation(self, now: float) -> Observation | None:
        if self.hex is None or self.estimated:
            return None
        on_ground = self.alt_ft <= 0
        return Observation(
            hex=self.hex,
            source="fr24",
            seen_at=min(now, self.pos_time),
            pos_time=min(now, self.pos_time),
            lat=self.lat,
            lon=self.lon,
            alt_baro_ft=None if on_ground else self.alt_ft,
            on_ground=on_ground,
            gs_kt=self.gs_kt,
            track_deg=self.track_deg,
            baro_rate_fpm=self.vrate_fpm,
            callsign=self.callsign,
            squawk=self.squawk,
            pos_source=POS_SOURCES.get(self.data_source, "other"),
            registration=self.registration,
            type_code=self.type_code,
        )


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        return None
    return float(v)


def _str(v: Any, upper: bool = True) -> str | None:
    if not isinstance(v, str) or not v.strip():
        return None
    return v.strip().upper() if upper else v.strip()


def _time(v: Any) -> float | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_flight(d: dict[str, Any]) -> FR24Flight | None:
    lat, lon, ts = _num(d.get("lat")), _num(d.get("lon")), _time(d.get("timestamp"))
    alt = _num(d.get("alt"))
    fid = d.get("fr24_id")
    if lat is None or lon is None or ts is None or alt is None or not isinstance(fid, str):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    hex_ = (_str(d.get("hex")) or "").lower()
    squawk = _str(d.get("squawk"))
    return FR24Flight(
        fr24_id=fid,
        hex=hex_ if _HEX.match(hex_) else None,
        callsign=_str(d.get("callsign")),
        registration=_str(d.get("reg")),
        type_code=_str(d.get("type")),
        flight=_str(d.get("flight")),
        lat=lat,
        lon=lon,
        alt_ft=alt,
        gs_kt=_num(d.get("gspeed")),
        track_deg=_num(d.get("track")),
        vrate_fpm=_num(d.get("vspeed")),
        squawk=squawk if squawk and _SQUAWK.match(squawk) and squawk != "0000" else None,
        pos_time=ts,
        data_source=_str(d.get("source")) or "UNKNOWN",
        painted_as=_str(d.get("painted_as")),
        operating_as=_str(d.get("operating_as")),
        orig_icao=_str(d.get("orig_icao")),
        orig_iata=_str(d.get("orig_iata")),
        dest_icao=_str(d.get("dest_icao")),
        dest_iata=_str(d.get("dest_iata")),
        eta=_time(d.get("eta")),
    )


def parse_positions(payload: Any) -> list[FR24Flight]:
    if not isinstance(payload, dict):
        raise SourceError("fr24: unexpected response shape")
    rows = payload.get("data") or []
    if not isinstance(rows, list):
        raise SourceError("fr24: unexpected response shape")
    return [f for r in rows if isinstance(r, dict) and (f := parse_flight(r)) is not None]


def to_batch(flights: list[FR24Flight], now: float) -> SourceBatch:
    batch = SourceBatch(source="fr24", fetched_at=now)
    batch.observations = [o for f in flights if (o := f.to_observation(now)) is not None]
    return batch


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("Retry-After")
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


class FR24Source:
    """Fetch and normalize only; budget, cap, breaker and cache live in `ingest/fr24.py`."""

    name = "fr24"
    # Explorer allows about 10 requests a minute (FR24's plan page; not in the API spec).
    max_rate_per_s = 10 / 60

    def __init__(self, http: httpx.AsyncClient, clock: Clock, api_key: str) -> None:
        self.http = http
        self.clock = clock
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Accept-Version": "v1",
            "Accept-Encoding": "gzip",
            "User-Agent": USER_AGENT,
        }

    async def _get(self, url: str, params: dict[str, str]) -> Any:
        try:
            resp = await self.http.get(url, params=params, headers=self._headers(), timeout=20)
        except httpx.HTTPError as e:
            raise SourceError(f"fr24: {type(e).__name__}") from e
        if resp.status_code != 200:
            raise SourceError(
                f"fr24: HTTP {resp.status_code}", resp.status_code, _retry_after(resp)
            )
        try:
            return resp.json()
        except ValueError as e:
            raise SourceError("fr24: invalid JSON") from e

    async def fetch_flights(
        self, registrations: list[str], callsigns: list[str]
    ) -> list[FR24Flight]:
        regs = [r.strip().upper() for r in registrations if valid_registration(r)]
        css = [c.strip().upper() for c in callsigns if valid_callsign(c)]
        regs, css = regs[:MAX_PER_PARAM], css[:MAX_PER_PARAM]
        if not regs and not css:
            return []
        params = {"limit": str(len(regs) + len(css))}  # bounds the credits a request can cost
        if regs:
            params["registrations"] = ",".join(regs)
        if css:
            params["callsigns"] = ",".join(css)
        return parse_positions(await self._get(POSITIONS_URL, params))

    async def fetch_usage(self, period: str = "30d") -> list[dict[str, Any]]:
        """Credits FR24 itself counted per endpoint over a rolling period (24h, 7d, 30d, 1y)."""
        payload = await self._get(USAGE_URL, {"period": period})
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise SourceError("fr24 usage: unexpected response shape")
        out = []
        for r in rows:
            if isinstance(r, dict) and isinstance(r.get("endpoint"), str):
                out.append(
                    {
                        "endpoint": r["endpoint"],
                        "requests": int(_num(r.get("request_count")) or 0),
                        "credits": _num(r.get("credits")) or 0.0,
                    }
                )
        return out
