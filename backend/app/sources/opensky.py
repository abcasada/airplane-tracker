"""OpenSky Network REST API: state vectors in a bounding box, with OAuth2 client credentials.

Metered in API credits per day (4,000 for a standard user, 8,000 for an active feeder); a
`/states/all` box costs 1–4 credits by its area in square degrees. The poller keeps to half the
daily credits. Docs: https://openskynetwork.github.io/opensky-api/rest.html
"""

from __future__ import annotations

import math
from typing import Any

import httpx

from app.clock import Clock

from .base import AreaQuery, Observation, PosSource, SourceBatch, SourceError

AUTH_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
)
STATES_URL = "https://opensky-network.org/api/states/all"
MAX_POSITION_AGE_S = 60.0
TOKEN_REFRESH_MARGIN_S = 60.0
M_TO_FT = 3.280839895
MS_TO_KT = 1.943844492
MS_TO_FPM = 196.8503937
# OpenSky's extended category numbers → ADS-B emitter categories.
CATEGORIES = {
    2: "A1", 3: "A2", 4: "A3", 5: "A4", 6: "A5", 7: "A6", 8: "A7", 9: "B1", 10: "B2", 11: "B3",
    12: "B4", 14: "B6", 15: "B7", 16: "C1", 17: "C2", 18: "C3", 19: "C4", 20: "C5",
}  # fmt: skip
POS_SOURCES: dict[int, PosSource] = {0: "adsb", 2: "mlat"}


def bbox(q: AreaQuery) -> tuple[float, float, float, float]:
    """(lamin, lomin, lamax, lomax) around the query circle."""
    dlat = q.radius_nm / 60.0
    dlon = q.radius_nm / (60.0 * max(math.cos(math.radians(q.lat)), 0.01))
    lamin, lamax = max(-90.0, q.lat - dlat), min(90.0, q.lat + dlat)
    lomin, lomax = max(-180.0, q.lon - dlon), min(180.0, q.lon + dlon)
    return round(lamin, 4), round(lomin, 4), round(lamax, 4), round(lomax, 4)


def credits_for(q: AreaQuery) -> float:
    lamin, lomin, lamax, lomax = bbox(q)
    area = (lamax - lamin) * (lomax - lomin)
    if area <= 25:
        return 1.0
    if area <= 100:
        return 2.0
    if area <= 400:
        return 3.0
    return 4.0


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        return None
    return float(v)


def _scaled(v: Any, factor: float) -> float | None:
    n = _num(v)
    return n * factor if n is not None else None


def parse_state(sv: list[Any], now: float) -> Observation | None:
    if len(sv) < 17 or not isinstance(sv[0], str) or not sv[0].strip():
        return None
    last_contact = _num(sv[4])
    obs = Observation(
        hex=sv[0].strip().lower(), source="opensky", seen_at=min(now, last_contact or now)
    )
    lon, lat, t_pos = _num(sv[5]), _num(sv[6]), _num(sv[3])
    if (
        lat is not None and lon is not None and t_pos is not None
        and -90 <= lat <= 90 and -180 <= lon <= 180 and now - t_pos <= MAX_POSITION_AGE_S
    ):  # fmt: skip
        obs.lat, obs.lon, obs.pos_time = lat, lon, min(now, t_pos)
    obs.on_ground = sv[8] is True
    if not obs.on_ground:
        obs.alt_baro_ft = _scaled(sv[7], M_TO_FT)
    obs.alt_geom_ft = _scaled(sv[13], M_TO_FT) if len(sv) > 13 else None
    obs.gs_kt = _scaled(sv[9], MS_TO_KT)
    obs.track_deg = _num(sv[10])
    obs.baro_rate_fpm = _scaled(sv[11], MS_TO_FPM)
    if isinstance(sv[1], str) and sv[1].strip():
        obs.callsign = sv[1].strip().upper()
    if len(sv) > 14 and isinstance(sv[14], str) and sv[14].strip():
        obs.squawk = sv[14].strip()
    src = sv[16] if len(sv) > 16 else None
    obs.pos_source = POS_SOURCES.get(src, "other") if isinstance(src, int) else "other"
    cat = sv[17] if len(sv) > 17 else None
    if isinstance(cat, int):
        obs.category = CATEGORIES.get(cat)
    return obs


def parse_states(payload: Any, now: float) -> SourceBatch:
    if not isinstance(payload, dict):
        raise SourceError("opensky: unexpected response shape")
    batch = SourceBatch(source="opensky", fetched_at=now)
    for sv in payload.get("states") or []:
        if isinstance(sv, list) and (obs := parse_state(sv, now)):
            batch.observations.append(obs)
    return batch


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("X-Rate-Limit-Retry-After-Seconds")
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


class OpenSkySource:
    name = "opensky"
    max_cost = 4.0

    def __init__(
        self,
        http: httpx.AsyncClient,
        clock: Clock,
        client_id: str,
        client_secret: str,
        daily_credits: int = 4000,
    ) -> None:
        self.http = http
        self.clock = clock
        self.client_id = client_id
        self.client_secret = client_secret
        self.max_rate_per_s = daily_credits / 86400.0  # credits per second
        self._token: str | None = None
        self._token_expires = 0.0
        self.credits_remaining: int | None = None

    def cost(self, query: AreaQuery) -> float:
        return credits_for(query)

    def status_extra(self) -> dict[str, Any]:
        return {"credits_remaining": self.credits_remaining}

    async def _access_token(self) -> str:
        if self._token is not None and self.clock.now() < self._token_expires:
            return self._token
        try:
            resp = await self.http.post(
                AUTH_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                timeout=15,
            )
        except httpx.HTTPError as e:
            raise SourceError(f"opensky token: {type(e).__name__}") from e
        if resp.status_code != 200:
            raise SourceError(f"opensky token: HTTP {resp.status_code}", resp.status_code)
        try:
            body = resp.json()
            token = str(body["access_token"])
            expires_in = float(body.get("expires_in", 1800))
        except (ValueError, KeyError, TypeError) as e:
            raise SourceError("opensky token: invalid response") from e
        self._token = token
        self._token_expires = self.clock.now() + max(0.0, expires_in - TOKEN_REFRESH_MARGIN_S)
        return token

    async def _get_states(self, query: AreaQuery) -> httpx.Response:
        lamin, lomin, lamax, lomax = bbox(query)
        params: dict[str, str | int | float] = {
            "lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax, "extended": 1,
        }  # fmt: skip
        token = await self._access_token()
        try:
            return await self.http.get(
                STATES_URL,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"},
                timeout=15,
            )
        except httpx.HTTPError as e:
            raise SourceError(f"opensky: {type(e).__name__}") from e

    async def fetch(self, query: AreaQuery) -> SourceBatch:
        resp = await self._get_states(query)
        if resp.status_code == 401:  # token expired early or was revoked: renew once
            self._token = None
            resp = await self._get_states(query)
        remaining = resp.headers.get("X-Rate-Limit-Remaining")
        if remaining is not None and remaining.isdigit():
            self.credits_remaining = int(remaining)
        if resp.status_code != 200:
            raise SourceError(
                f"opensky: HTTP {resp.status_code}", resp.status_code, _retry_after(resp)
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise SourceError("opensky: invalid JSON") from e
        return parse_states(payload, self.clock.now())
