"""In-process event hub for WebSocket fan-out.

Every stage/log line a job emits is published here as a JobEvent. WebSocket
connections subscribe and get every event; they can also filter by job_id client
side. Using an asyncio.Queue per subscriber means a slow/absent WebSocket client
never blocks the job pipeline (we drop events for that client, not the job).

This registry is process-local and intentionally simple for v1: if the app runs
with multiple uvicorn workers, events stay per-worker. That's an accepted
hackathon limitation.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import TYPE_CHECKING

from ..contracts import JobEvent, JobStage

if TYPE_CHECKING:
    from asyncio import Queue


class EventHub:
    """Broadcasts JobEvents to all registered subscribers.

    Each subscriber gets an asyncio.Queue; publish() puts a copy on every queue.
    A full queue means that subscriber is slow — we drop the event for it only,
    never back up the job pipeline.
    """

    def __init__(self) -> None:
        self._subscribers: set[Queue] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> Queue:
        """Register a new subscriber and return its event queue."""
        q: Queue = asyncio.Queue()
        async with self._lock:
            self._subscribers.add(q)
        return q

    async def unsubscribe(self, q: Queue) -> None:
        async with self._lock:
            self._subscribers.discard(q)

    async def publish(self, event: JobEvent) -> None:
        """Push an event to every subscriber, dropping for slow ones."""
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - sanity guard
                pass


# Single shared hub for the whole process.
hub = EventHub()
