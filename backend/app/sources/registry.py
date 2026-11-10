"""Which live sources are enabled: the free aggregators always, the others when configured."""

import httpx

from app.clock import Clock
from app.config import Settings

from . import readsb
from .base import AreaSource, FeedSource


def build_sources(
    settings: Settings, http: httpx.AsyncClient, clock: Clock
) -> list[AreaSource | FeedSource]:
    """In lookup-preference order: followed-aircraft lookups go to the first healthy one."""
    sources: list[AreaSource | FeedSource] = [
        readsb.adsbfi(http, clock),
        readsb.adsblol(http, clock),
    ]
    return sources
