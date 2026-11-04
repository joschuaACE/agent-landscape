"""Tiny async broadcast bus feeding the UI websocket clients."""

from __future__ import annotations

import asyncio
import time
from typing import Any


class EventBus:
    def __init__(self, max_queue: int = 2000) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._max = max_queue

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def emit(self, type_: str, **data: Any) -> None:
        event = {"type": type_, "ts": time.time(), **data}
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # slow client – drop
                pass
