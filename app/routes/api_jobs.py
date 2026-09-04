"""Frontend-facing job API under /api/* (matches the React client contract)."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services.frontend_adapter import job_summary, job_to_frontend
from ..services.github import InvalidRepoURL, parse_github_url
from ..services.jobs import JobNotFound, get_job, list_jobs, make_job, schedule_job

router = APIRouter(prefix="/api", tags=["frontend-api"])


class CreateJobBody(BaseModel):
    repo_url: str = Field(..., description="GitHub repo URL")
    target_provider: Optional[str] = Field(
        default="render", description="Deploy target hint (render|railway|fly)"
    )


@router.post("/jobs", status_code=202)
async def create_job(payload: CreateJobBody) -> dict[str, Any]:
    try:
        parse_github_url(payload.repo_url)
    except InvalidRepoURL as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = make_job(payload.repo_url)
    await schedule_job(job)
    return job_to_frontend(job)


@router.get("/jobs")
async def get_jobs() -> dict[str, Any]:
    jobs = await list_jobs()
    # Newest first
    summaries = [job_summary(j) for j in jobs]
    summaries.sort(key=lambda s: s.get("created_at") or "", reverse=True)
    return {"jobs": summaries}


@router.get("/jobs/{job_id}")
async def read_job(job_id: str) -> dict[str, Any]:
    try:
        job = await get_job(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    return job_to_frontend(job)
