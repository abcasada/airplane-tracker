"""Your own receiver: readsb/ultrafeeder `aircraft.json` (1090 MHz) and, optionally, dump978's
(978 MHz UAT) over the LAN. No query area and no rate limit: it returns everything it hears."""

import httpx

from app.clock import Clock

from .base import SourceBatch, SourceError
from .readsb import parse_payload


class ReceiverSource:
    def __init__(
        self, name: str, url: str, http: httpx.AsyncClient, clock: Clock, interval_s: float = 1.0
    ) -> None:
        self.name = name
        self.url = url
        self.http = http
        self.clock = clock
        self.interval_s = interval_s

    async def fetch_all(self) -> SourceBatch:
        try:
            resp = await self.http.get(self.url, timeout=5)
        except httpx.HTTPError as e:
            raise SourceError(f"{self.name}: {type(e).__name__}") from e
        if resp.status_code != 200:
            raise SourceError(f"{self.name}: HTTP {resp.status_code}", resp.status_code)
        try:
            payload = resp.json()
        except ValueError as e:
            raise SourceError(f"{self.name}: invalid JSON") from e
        batch = parse_payload(payload, self.name, self.clock.now(), local_clock=True)
        if self.name == "receiver-uat":
            # dump978 doesn't say how a position was obtained; UAT positions are ADS-B.
            for o in batch.observations:
                if o.lat is not None and o.pos_source == "other":
                    o.pos_source = "adsb"
        return batch
