"""Background services: polling, enrichment, filter evaluation and notifications.

Phase 0 only reloads places; the ingest pipeline attaches here in Phase 1.
"""

import asyncio
import logging

from app.context import AppContext
from app.places.models import Place
from app.places.repo import PlaceRepo

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.places: list[Place] = []
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        await self.reload_places()

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def reload_places(self) -> None:
        self.places = await PlaceRepo(self.ctx.db, self.ctx.clock).list()
