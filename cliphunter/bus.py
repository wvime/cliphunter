"""In-process event bus. A Redis implementation can replace it later."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol

from cliphunter.jsonlog import log_event
from cliphunter.timeutil import utc_iso

STREAM_START = "stream.start"
STREAM_END = "stream.end"
SEGMENT_READY = "segment.ready"
CLIP_CREATED = "clip.created"
METRICS_SAMPLE = "metrics.sample"


@dataclass(frozen=True)
class Event:
    type: str
    payload: dict[str, Any]
    ts: str = field(default_factory=utc_iso)


class EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...

    async def subscribe(self, *event_types: str) -> asyncio.Queue[Event]: ...

    async def close(self) -> None: ...


class InMemoryEventBus:
    """asyncio.Queue fan-out. Slow subscribers drop their oldest event."""

    def __init__(self, maxsize: int = 1000) -> None:
        self._maxsize = maxsize
        self._subs: dict[str, list[asyncio.Queue[Event]]] = defaultdict(list)
        self._all: list[asyncio.Queue[Event]] = []
        self._lock = asyncio.Lock()
        self.dropped = 0

    async def subscribe(self, *event_types: str) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._maxsize)
        async with self._lock:
            if not event_types:
                self._all.append(queue)
            else:
                for name in event_types:
                    self._subs[name].append(queue)
        return queue

    async def publish(self, event: Event) -> None:
        async with self._lock:
            targets = list(self._subs.get(event.type, [])) + list(self._all)
        for queue in targets:
            self._offer(queue, event)

    def _offer(self, queue: asyncio.Queue[Event], event: Event) -> None:
        try:
            queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass
        self.dropped += 1
        if self.dropped == 1 or self.dropped % 100 == 0:
            log_event("WARNING", "bus.drop", dropped=self.dropped, event_type=event.type)

    async def close(self) -> None:
        async with self._lock:
            self._subs.clear()
            self._all.clear()
