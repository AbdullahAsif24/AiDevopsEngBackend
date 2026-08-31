"""WebSocket route that streams JobEvents to the frontend.

The event payload is the stable contract:
    {job_id, stage, message, timestamp}

Clients can optionally filter to a single job by sending a message containing a
job_id, or just listen to everything (and filter client-side).
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..services.events import hub

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/jobs")
async def ws_jobs(websocket: WebSocket) -> None:
    """Live event stream for job lifecycle updates."""
    await websocket.accept()

    # Subscribe to the shared hub; we get every JobEvent on this queue.
    queue = await hub.subscribe()
    filtered_job_id: str | None = None

    try:
        # Read a filter message (optional) without blocking the receive loop:
        # poll our event queue and also watch for an incoming filter.
        while True:
            # Pull the next event; use a short timeout so we can also check for
            # client messages / disconnects in between.
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                event = None

            if event is not None and (filtered_job_id is None or event.job_id == filtered_job_id):
                await websocket.send_json(event.model_dump())

            # Opportunistically accept a filter message if the client sent one.
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=0.05)
                filtered_job_id = msg.strip() or None
            except Exception:
                pass  # no message right now; keep streaming

    except WebSocketDisconnect:
        pass
    finally:
        await hub.unsubscribe(queue)
        try:
            await websocket.close()
        except Exception:
            pass
