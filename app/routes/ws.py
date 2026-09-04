"""WebSocket route that streams JobEvents to the frontend.

Payload matches the React client contract (mapped stages + status field):
    {job_id, stage, status, message, timestamp, data?}

Clients filter to a single job by sending the job_id as a text message after connect,
or connect to /ws/jobs/{job_id} which pre-filters.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ..services.events import hub
from ..services.frontend_adapter import live_event_to_frontend
from ..services.jobs import JobNotFound, get_job

router = APIRouter(tags=["websocket"])


async def _stream(websocket: WebSocket, filtered_job_id: str | None) -> None:
    await websocket.accept()
    queue = await hub.subscribe()
    last_fe_stage: str | None = None

    try:
        while True:
            if websocket.client_state != WebSocketState.CONNECTED:
                break

            get_event = asyncio.create_task(queue.get())
            get_msg = asyncio.create_task(websocket.receive_text())
            done, pending = await asyncio.wait(
                {get_event, get_msg},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=30.0,
            )

            # Heartbeat timeout with no activity — keep waiting.
            if not done:
                for t in pending:
                    t.cancel()
                continue

            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

            if get_msg in done:
                try:
                    msg = get_msg.result()
                    filtered_job_id = (msg or "").strip() or filtered_job_id
                    last_fe_stage = None
                except WebSocketDisconnect:
                    break
                except Exception:
                    break

            if get_event in done:
                try:
                    event = get_event.result()
                except Exception:
                    continue

                if filtered_job_id is not None and event.job_id != filtered_job_id:
                    continue

                job = None
                try:
                    job = await get_job(event.job_id)
                except JobNotFound:
                    job = None

                fe = live_event_to_frontend(event, job)
                fe_stage = fe.get("stage")

                if (
                    last_fe_stage
                    and last_fe_stage not in ("done",)
                    and fe_stage != last_fe_stage
                    and fe.get("status") != "failed"
                ):
                    await websocket.send_json(
                        {
                            "job_id": event.job_id,
                            "stage": last_fe_stage,
                            "status": "success",
                            "message": f"{last_fe_stage} complete",
                            "timestamp": fe.get("timestamp"),
                        }
                    )

                await websocket.send_json(fe)
                last_fe_stage = fe_stage if fe_stage != "done" else "done"

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.unsubscribe(queue)
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.close()
            except Exception:
                pass


@router.websocket("/ws/jobs")
async def ws_jobs(websocket: WebSocket) -> None:
    await _stream(websocket, filtered_job_id=None)


@router.websocket("/ws/jobs/{job_id}")
async def ws_jobs_by_id(websocket: WebSocket, job_id: str) -> None:
    await _stream(websocket, filtered_job_id=job_id)
