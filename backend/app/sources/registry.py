"""Which live sources are enabled: the free aggregators always, the others when configured."""

import httpx

from app.clock import Clock
from app.config import Settings

from . import readsb
from .base import AreaSource, FeedSource
from .opensky import OpenSkySource


def build_sources(
    settings: Settings, http: httpx.AsyncClient, clock: Clock
) -> list[AreaSource | FeedSource]:
    """In lookup-preference order: followed-aircraft lookups go to the first healthy one."""
    sources: list[AreaSource | FeedSource] = [
        readsb.adsbfi(http, clock),
        readsb.adsblol(http, clock),
    ]
    if settings.flyitaly_api_key:
        sources.append(readsb.flyitaly(http, clock, settings.flyitaly_api_key))
    if settings.opensky_client_id and settings.opensky_client_secret:
        sources.append(
            OpenSkySource(
                http, clock, settings.opensky_client_id, settings.opensky_client_secret,
                settings.opensky_daily_credits,
            )
        )  # fmt: skip
    return sources
