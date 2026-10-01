"""HTTP routes for the AI DevOps job API."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from ..auth import get_user_id
from ..contracts import JobStatus
from ..services.github import InvalidRepoURL, parse_github_url
from ..services.jobs import JobNotFound, get_job, list_jobs, make_job, schedule_job

router = APIRouter(prefix="/jobs", tags=["jobs"])


class CreateJobRequest(BaseModel):
    """Request body for POST /jobs."""

    repo_url: str = Field(..., description="GitHub repo URL, e.g. https://github.com/owner/repo")
    env_vars: dict[str, str] = Field(default_factory=dict, description="Optional environment variables")


class CreateJobResponse(BaseModel):
    """Response body for POST /jobs — we never block on the work itself."""

    job_id: str
    status: str
    repo_url: str = ""
    created_at: str | None = None
    logs: list = Field(default_factory=list)


@router.post("", response_model=CreateJobResponse, status_code=202)
async def create_job(
    payload: CreateJobRequest, user_id: str = Depends(get_user_id)
) -> CreateJobResponse:
    """Validate the repo URL and kick off an async background job.

    Returns 202 Accepted with a job_id immediately; the actual clone/analyze/
    generate work happens in the background, not during this request.

    Args:
        payload: Job creation request with repo URL and optional env vars
        user_id: The authenticated user's ID

    Returns:
        CreateJobResponse with job_id and initial status
    """
    # Validate the URL shape BEFORE anything else so we fail fast on typos.
    try:
        parse_github_url(payload.repo_url)
    except InvalidRepoURL as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    job = make_job(user_id, payload.repo_url)
    await schedule_job(job, user_id)

    # Store environment variables if provided
    if payload.env_vars:
        from ..services.env_vars import get_env_var_manager
        env_var_manager = get_env_var_manager()
        is_valid, errors = env_var_manager.validate_env_vars(payload.env_vars)
        if not is_valid:
            raise HTTPException(
                status_code=400, detail={"message": "Invalid environment variables", "errors": errors}
            )
        await env_var_manager.store_env_vars(job.job_id, payload.env_vars)

    return CreateJobResponse(
        job_id=job.job_id,
        status=job.status.value,
        repo_url=job.repo_url,
        created_at=job.created_at.isoformat(),
        logs=[],
    )


@router.get("", response_model=list[JobStatus])
async def read_jobs(user_id: str = Depends(get_user_id)) -> list[JobStatus]:
    """List all known jobs for the authenticated user (most recent first) for the History view.

    Args:
        user_id: The authenticated user's ID

    Returns:
        List of JobStatus objects for the user's jobs
    """
    return await list_jobs(user_id)


@router.get("/{job_id}", response_model=JobStatus)
async def read_job(
    job_id: str, user_id: str = Depends(get_user_id)
) -> JobStatus:
    """Return the current snapshot (status, logs, result/error) for a job.

    Args:
        job_id: The job identifier
        user_id: The authenticated user's ID

    Returns:
        JobStatus with current state

    Raises:
        HTTPException: If job not found or user doesn't have access
    """
    try:
        job = await get_job(job_id)
        # Verify user has access to this job
        from ..services.database import get_db_service
        db = get_db_service()
        job_record = await db.get_job(job_id)
        if job_record and job_record.get("user_id") != user_id:
            raise HTTPException(status_code=403, detail="You don't have access to this job")
        return job
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
