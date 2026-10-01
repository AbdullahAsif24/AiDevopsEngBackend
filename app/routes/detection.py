"""HTTP routes for deployment-type detection.

Exposes:
  * POST /jobs/{job_id}/detect            -> run detection on a job's cloned repo
  * POST /jobs/{job_id}/override-detection -> manually confirm a type for an
                                               AMBIGUOUS job (resumes pipeline)

Both require an existing job. The detect endpoint additionally requires the job
to have a cloned repo_path (jobs are created and cloned by the background worker;
if a job hasn't been cloned yet we 409 so the caller knows to wait).
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..contracts import DeploymentType, DetectionResult
from ..services.jobs import (
    JobNotFound,
    get_job,
    validate_detection,
)

router = APIRouter(prefix="/jobs", tags=["jobs", "detection"])


class OverrideDetectionRequest(BaseModel):
    """Request body for POST /jobs/{id}/override-detection."""

    deployment_type: DeploymentType = Field(
        ..., description="The manually confirmed deployment type."
    )


@router.post("/{job_id}/detect", response_model=DetectionResult)
async def detect_job(job_id: str) -> DetectionResult:
    """Run deployment-type detection for a job's cloned repo.

    Persists the result onto the job and emits a WebSocket detection event.
    Returns the DetectionResult as JSON.

    Raises 400 if the repo path is missing/invalid, 404 if the job is unknown.
    """
    try:
        job = await get_job(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc

    repo_path = job.repo_path
    if not repo_path:
        raise HTTPException(
            status_code=400,
            detail="Job has not been cloned yet; detection requires a cloned repo.",
        )

    if not os.path.isdir(repo_path):
        raise HTTPException(
            status_code=400,
            detail="Cloned repo path no longer exists for this job.",
        )

    result = await validate_detection(job, repo_path)
    # validate_detection returns None when the type is ambiguous (paused for
    # review), but the job record still holds the result — reconstruct it so the
    # endpoint always returns a valid DetectionResult.
    if result is None and job.detection is not None:
        d = job.detection
        result = DetectionResult(
            deployment_type=d.deployment_type,
            confidence="low" if d.deployment_type == DeploymentType.AMBIGUOUS else "high",
            detected_framework=d.detected_framework,
            entry_point=d.entry_point,
            listen_port=d.listen_port,
            reasoning=d.reasoning,
            needs_dockerfile=d.needs_dockerfile,
            ambiguous_reason=d.reasoning,
            detection_method="llm" if d.detection_method == "llm" else "rule_based",
        )
    return result


@router.post("/{job_id}/override-detection", response_model=DetectionResult)
async def override_job_detection(
    job_id: str, payload: OverrideDetectionRequest
) -> DetectionResult:
    """Manually confirm a deployment type for an AMBIGUOUS job.

    Resumes the paused pipeline so Dockerfile generation / Vercel deploy can
    proceed with the user's chosen type.
    """
    from ..services.jobs import override_detection

    try:
        return await override_detection(job_id, payload.deployment_type)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc