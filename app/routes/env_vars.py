"""Environment variable routes for job configuration."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import get_user_id
from ..services.env_vars import EnvVarManager, get_env_var_manager

router = APIRouter(prefix="/env-vars", tags=["env-vars"])


class EnvVarRequest(BaseModel):
    """Request body for setting environment variables."""

    job_id: str
    env_vars: dict[str, str]


class EnvVarResponse(BaseModel):
    """Response for environment variable operations."""

    job_id: str
    env_vars: dict[str, str]
    message: str


@router.post("")
async def set_env_vars(
    request: EnvVarRequest, user_id: str = Depends(get_user_id)
) -> EnvVarResponse:
    """Set environment variables for a job.

    Args:
        request: Contains job_id and environment variables
        user_id: The authenticated user's ID

    Returns:
        Confirmation of stored environment variables
    """
    # Verify the job belongs to the user
    from ..services.database import get_db_service

    db = get_db_service()
    job = await db.get_job(request.job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="You don't have access to this job")

    # Validate environment variables
    env_var_manager = get_env_var_manager()
    is_valid, errors = env_var_manager.validate_env_vars(request.env_vars)

    if not is_valid:
        raise HTTPException(
            status_code=400, detail={"message": "Invalid environment variables", "errors": errors}
        )

    # Store environment variables
    await env_var_manager.store_env_vars(request.job_id, request.env_vars)

    return EnvVarResponse(
        job_id=request.job_id,
        env_vars=request.env_vars,
        message="Environment variables stored successfully",
    )


@router.get("/{job_id}")
async def get_env_vars(job_id: str, user_id: str = Depends(get_user_id)) -> dict[str, str]:
    """Get environment variables for a job.

    Args:
        job_id: The job identifier
        user_id: The authenticated user's ID

    Returns:
        Dictionary of environment variables
    """
    # Verify the job belongs to the user
    from ..services.database import get_db_service

    db = get_db_service()
    job = await db.get_job(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="You don't have access to this job")

    # Get environment variables
    env_var_manager = get_env_var_manager()
    env_vars = await env_var_manager.get_env_vars(job_id)

    return env_vars