"""HTTP routes for deployment status monitoring.

Exposes:
  * GET /deployments/{service_id}/status -> Get Render deployment status
  * GET /jobs/{job_id}/deployment -> Get deployment info for a job
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..contracts import DeploymentResult
from ..services.jobs import JobNotFound, get_job
from ..services.render_deploy import RenderDeployError, get_deployment_status

router = APIRouter(prefix="/deployments", tags=["deployments"])


@router.get("/{service_id}/status")
async def get_render_deployment_status(service_id: str) -> dict:
    """Get the current deployment status of a Render service.

    Args:
        service_id: Render service ID

    Returns:
        Dictionary with deployment status information

    Raises:
        404: If service not found
        500: If status check fails
    """
    try:
        status = await get_deployment_status(service_id)
        return status
    except RenderDeployError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/jobs/{job_id}/deployment")
async def get_job_deployment(job_id: str) -> DeploymentResult:
    """Get deployment information for a specific job.

    Args:
        job_id: Job ID

    Returns:
        DeploymentResult with deployment details

    Raises:
        404: If job not found or no deployment exists
    """
    try:
        job = await get_job(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc

    if not job.deployment:
        raise HTTPException(
            status_code=404,
            detail="No deployment information available for this job"
        )

    return job.deployment
