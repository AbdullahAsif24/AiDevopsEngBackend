"""Map backend JobStatus / JobEvent shapes onto the frontend API contract.

Frontend (Vite) expects:
  stages: queued | clone | analyze | generate | build | self_heal | deploy | done
  job status: queued | running | succeeded | failed
  events with status: running | success | failed | info
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..contracts import JobEvent, JobStage, JobStatus

_STAGE_MAP: dict[str, str] = {
    JobStage.QUEUED.value: "queued",
    JobStage.CLONING.value: "clone",
    JobStage.ANALYZING.value: "analyze",
    JobStage.GENERATING.value: "generate",
    JobStage.BUILDING.value: "build",
    JobStage.HEALING.value: "self_heal",
    JobStage.DONE.value: "done",
    JobStage.FAILED.value: "done",
}


def _iso(ts: datetime | None) -> str:
    if ts is None:
        return datetime.now(timezone.utc).isoformat()
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc).isoformat()
    return ts.isoformat()


def frontend_stage(backend_stage: str, message: str = "") -> str:
    msg = (message or "").lower()
    if backend_stage == JobStage.BUILDING.value and (
        "deploy" in msg or "render" in msg or "pushing" in msg or "docker hub" in msg
    ):
        return "deploy"
    return _STAGE_MAP.get(backend_stage, backend_stage)


def map_overall_status(job: JobStatus) -> str:
    if job.status == JobStage.FAILED or job.error:
        return "failed"
    if job.status == JobStage.DONE:
        return "succeeded"
    if job.status == JobStage.QUEUED:
        return "queued"
    return "running"


def _event_payload(
    *,
    job_id: str,
    stage: str,
    status: str,
    message: str,
    timestamp: str,
    data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "job_id": job_id,
        "stage": stage,
        "status": status,
        "message": message,
        "timestamp": timestamp,
    }
    if data:
        out["data"] = data
    return out


def synthesize_frontend_events(job: JobStatus) -> list[dict[str, Any]]:
    """Turn backend log lines into frontend events with running/success pairs."""
    events: list[dict[str, Any]] = []
    prev_fe_stage: str | None = None
    prev_ts: str | None = None

    for log in job.logs:
        fe_stage = frontend_stage(log.stage.value, log.message)
        ts = _iso(log.timestamp)

        if prev_fe_stage and prev_fe_stage not in ("done",) and prev_fe_stage != fe_stage:
            events.append(
                _event_payload(
                    job_id=job.job_id,
                    stage=prev_fe_stage,
                    status="success",
                    message=f"{prev_fe_stage} complete",
                    timestamp=prev_ts or ts,
                )
            )

        if log.stage == JobStage.FAILED:
            if prev_fe_stage and prev_fe_stage != "done":
                events.append(
                    _event_payload(
                        job_id=job.job_id,
                        stage=prev_fe_stage,
                        status="failed",
                        message=log.message,
                        timestamp=ts,
                    )
                )
            events.append(
                _event_payload(
                    job_id=job.job_id,
                    stage="done",
                    status="failed",
                    message=log.message,
                    timestamp=ts,
                    data={"reason": job.error or log.message},
                )
            )
            prev_fe_stage = "done"
            prev_ts = ts
            continue

        if log.stage == JobStage.DONE:
            data: dict[str, Any] = {}
            if job.deploy_url:
                data["url"] = job.deploy_url
                data["provider"] = "render"
            if job.result and job.result.dockerfile_content:
                data["dockerfile"] = job.result.dockerfile_content
            if job.result:
                data.setdefault("language", job.result.language)
                data.setdefault("framework", str(job.result.framework.value if hasattr(job.result.framework, "value") else job.result.framework))
                if job.result.port:
                    data["port"] = job.result.port
                if job.result.entry_point:
                    data["entrypoint"] = job.result.entry_point
            events.append(
                _event_payload(
                    job_id=job.job_id,
                    stage="done",
                    status="success",
                    message=log.message,
                    timestamp=ts,
                    data=data or None,
                )
            )
            prev_fe_stage = "done"
            prev_ts = ts
            continue

        # Normal progress line → running for that frontend stage
        events.append(
            _event_payload(
                job_id=job.job_id,
                stage=fe_stage,
                status="running",
                message=log.message,
                timestamp=ts,
            )
        )
        prev_fe_stage = fe_stage
        prev_ts = ts

    return events


def job_to_frontend(job: JobStatus) -> dict[str, Any]:
    """Full Job object expected by the React client."""
    created = _iso(job.logs[0].timestamp) if job.logs else _iso(None)
    updated = _iso(job.logs[-1].timestamp) if job.logs else created
    return {
        "job_id": job.job_id,
        "status": map_overall_status(job),
        "repo_url": job.repo_url,
        "deployed_url": job.deploy_url,
        "provider": "render" if job.deploy_url else None,
        "created_at": created,
        "updated_at": updated,
        "events": synthesize_frontend_events(job),
    }


def job_summary(job: JobStatus) -> dict[str, Any]:
    created = _iso(job.logs[0].timestamp) if job.logs else _iso(None)
    return {
        "job_id": job.job_id,
        "repo_url": job.repo_url,
        "status": map_overall_status(job),
        "deployed_url": job.deploy_url,
        "provider": "render" if job.deploy_url else None,
        "created_at": created,
    }


def live_event_to_frontend(event: JobEvent, job: Optional[JobStatus] = None) -> dict[str, Any]:
    """Single live WebSocket frame → frontend JobEvent (status=running/failed/success)."""
    fe_stage = frontend_stage(event.stage.value, event.message)
    ts = _iso(event.timestamp)

    if event.stage == JobStage.FAILED:
        return _event_payload(
            job_id=event.job_id,
            stage="done",
            status="failed",
            message=event.message,
            timestamp=ts,
            data={"reason": event.message},
        )

    if event.stage == JobStage.DONE:
        data: dict[str, Any] = {}
        if job and job.deploy_url:
            data["url"] = job.deploy_url
            data["provider"] = "render"
        if job and job.result and job.result.dockerfile_content:
            data["dockerfile"] = job.result.dockerfile_content
        return _event_payload(
            job_id=event.job_id,
            stage="done",
            status="success",
            message=event.message,
            timestamp=ts,
            data=data or None,
        )

    return _event_payload(
        job_id=event.job_id,
        stage=fe_stage,
        status="running",
        message=event.message,
        timestamp=ts,
    )
