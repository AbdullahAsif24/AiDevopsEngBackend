"""HTTP routes for the AI DevOps job API."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..contracts import JobStatus
from ..services.github import InvalidRepoURL, parse_github_url
from ..services.jobs import JobNotFound, get_job, make_job, schedule_job

router = APIRouter(prefix="/jobs", tags=["jobs"])


class CreateJobRequest(BaseModel):
    """Request body for POST /jobs."""

    repo_url: str = Field(..., description="GitHub repo URL, e.g. https://github.com/owner/repo")


class CreateJobResponse(BaseModel):
    """Response body for POST /jobs — we never block on the work itself."""

    job_id: str
    status: str


@router.post("", response_model=CreateJobResponse, status_code=202)
async def create_job(payload: CreateJobRequest) -> CreateJobResponse:
    """Validate the repo URL and kick off an async background job.

    Returns 202 Accepted with a job_id immediately; the actual clone/analyze/
    generate work happens in the background, not during this request.
    """
    # Validate the URL shape BEFORE anything else so we fail fast on typos.
    try:
        parse_github_url(payload.repo_url)
    except InvalidRepoURL as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    job = make_job(payload.repo_url)
    await schedule_job(job)
    return CreateJobResponse(job_id=job.job_id, status=job.status.value)


@router.get("/{job_id}", response_model=JobStatus)
async def read_job(job_id: str) -> JobStatus:
    """Return the current snapshot (status, logs, result/error) for a job."""
    try:
        return await get_job(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
