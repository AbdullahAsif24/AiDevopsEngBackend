"""HTTP routes for deployment history and basic rollback."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..contracts import DeploymentRecord
from ..services import supabase_store as db

router = APIRouter(prefix="/deployments", tags=["deployments"])


class RollbackResponse(BaseModel):
    ok: bool
    deployment: Optional[DeploymentRecord] = None
    detail: str = ""


def _to_record(row: dict) -> DeploymentRecord:
    return DeploymentRecord(
        id=str(row.get("id", "")),
        job_id=row.get("job_id"),
        provider=row.get("provider") or "render",
        service_id=row.get("service_id"),
        live_url=row.get("live_url"),
        image_tag=row.get("image_tag"),
        status=row.get("status") or "unknown",
        is_active=bool(row.get("is_active")),
        created_at=row.get("created_at"),
    )


@router.get("", response_model=list[DeploymentRecord])
async def list_deployments(
    job_id: Optional[str] = Query(None, description="Filter by job id"),
    limit: int = Query(50, ge=1, le=200),
) -> list[DeploymentRecord]:
    """Return recent deployments (Supabase). Empty list when Supabase is unset."""
    if not db.supabase_configured():
        return []
    rows = await db.list_deployments(job_id=job_id, limit=limit)
    return [_to_record(r) for r in rows]


@router.get("/{deployment_id}", response_model=DeploymentRecord)
async def get_deployment(deployment_id: str) -> DeploymentRecord:
    if not db.supabase_configured():
        raise HTTPException(status_code=503, detail="Supabase is not configured")
    row = await db.get_deployment(deployment_id)
    if not row:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return _to_record(row)


@router.post("/{deployment_id}/rollback", response_model=RollbackResponse)
async def rollback_deployment(deployment_id: str) -> RollbackResponse:
    """Mark a previous deployment active and best-effort redeploy its image on Render."""
    if not db.supabase_configured():
        raise HTTPException(status_code=503, detail="Supabase is not configured")

    activated = await db.activate_deployment(deployment_id)
    if not activated:
        raise HTTPException(status_code=404, detail="Deployment not found or rollback failed")

    return RollbackResponse(
        ok=True,
        deployment=_to_record(activated),
        detail="Marked active; Render redeploy triggered when credentials + service_id exist",
    )
