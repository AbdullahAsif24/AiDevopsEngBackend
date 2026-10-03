"""HTTP routes for Auto-deploy on Push management."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_user_id
from ..contracts import (
    AutoDeployConfig,
    CreateAutoDeployRequest,
    TestPushRequest,
    UpdateAutoDeployRequest,
    WebhookEventRecord,
)
from ..services.auto_deploy import get_auto_deploy_manager

router = APIRouter(prefix="/auto-deploy", tags=["auto-deploy"])


@router.get("/configs", response_model=list[AutoDeployConfig])
async def list_configs(user_id: str = Depends(get_user_id)) -> list[AutoDeployConfig]:
    """List all auto-deploy configurations for the authenticated user."""
    manager = get_auto_deploy_manager()
    return await manager.list_configs(user_id)


@router.post("/configs", response_model=AutoDeployConfig, status_code=201)
async def create_config(
    payload: CreateAutoDeployRequest, user_id: str = Depends(get_user_id)
) -> AutoDeployConfig:
    """Create a new auto-deploy configuration with generated webhook secret and URL."""
    if not payload.repo_url:
        raise HTTPException(status_code=400, detail="Repository URL is required")

    manager = get_auto_deploy_manager()
    return await manager.create_config(user_id, payload)


@router.put("/configs/{config_id}", response_model=AutoDeployConfig)
async def update_config(
    config_id: str,
    payload: UpdateAutoDeployRequest,
    user_id: str = Depends(get_user_id),
) -> AutoDeployConfig:
    """Update auto-deploy branch, active toggle, or rollback setting."""
    manager = get_auto_deploy_manager()
    updated = await manager.update_config(config_id, user_id, payload)
    if not updated:
        raise HTTPException(status_code=404, detail="Configuration not found")
    return updated


@router.delete("/configs/{config_id}")
async def delete_config(
    config_id: str, user_id: str = Depends(get_user_id)
) -> dict[str, str]:
    """Delete an auto-deploy configuration."""
    manager = get_auto_deploy_manager()
    success = await manager.delete_config(config_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Configuration not found")
    return {"message": "Auto-deploy configuration deleted successfully"}


@router.get("/events", response_model=list[WebhookEventRecord])
async def list_events(user_id: str = Depends(get_user_id)) -> list[WebhookEventRecord]:
    """Get delivery history of push events and auto-deployment triggers."""
    manager = get_auto_deploy_manager()
    return await manager.list_events(user_id)


@router.post("/test-push", response_model=WebhookEventRecord)
async def test_push_event(
    payload: TestPushRequest, user_id: str = Depends(get_user_id)
) -> WebhookEventRecord:
    """Simulate a GitHub push event to test auto-deployment pipeline immediately."""
    manager = get_auto_deploy_manager()
    return await manager.trigger_test_push(
        user_id=user_id,
        config_id=payload.config_id,
        repo_url=payload.repo_url,
        branch=payload.branch or "main",
        commit_message=payload.commit_message or "Simulated commit: update production build",
        committer=payload.committer or "devops-engineer",
    )
