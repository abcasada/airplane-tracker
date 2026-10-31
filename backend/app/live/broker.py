"""Fan-out of live events to SSE clients. Queues are bounded; slow clients drop old events."""

import asyncio
import contextlib
import json
from typing import Any

QUEUE_SIZE = 20
MAX_CLIENTS = 20


class Broker:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[str]] = set()

    def subscribe(self) -> asyncio.Queue[str]:
        if len(self._subs) >= MAX_CLIENTS:
            raise RuntimeError("too many live connections")
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subs.discard(q)

    @property
    def clients(self) -> int:
        return len(self._subs)

    def publish(self, event: str, data: Any) -> None:
        if not self._subs:
            return
        msg = f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
        for q in self._subs:
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(msg)
