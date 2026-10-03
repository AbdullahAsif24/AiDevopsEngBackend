"""HTTP routes for Secrets Management Vault."""
from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import get_user_id
from ..contracts import (
    CreateSecretRequest,
    InviteTeamMemberRequest,
    RevealSecretResponse,
    RotateSecretRequest,
    SecretAuditLog,
    SecretEnvironment,
    SecretItem,
    TeamMember,
    UpdateSecretRequest,
)
from ..services.secrets_vault import get_secrets_vault_manager

router = APIRouter(prefix="/secrets", tags=["secrets"])


@router.get("", response_model=list[SecretItem])
async def list_secrets(
    environment: Optional[SecretEnvironment] = Query(None, description="Filter by environment"),
    user_id: str = Depends(get_user_id),
) -> list[SecretItem]:
    """List encrypted secrets with masked preview and rotation metadata."""
    manager = get_secrets_vault_manager()
    return await manager.list_secrets(user_id, environment)


@router.post("", response_model=SecretItem, status_code=201)
async def create_secret(
    payload: CreateSecretRequest,
    user_id: str = Depends(get_user_id),
) -> SecretItem:
    """Create and securely encrypt a new environment secret."""
    if not payload.key or not payload.value:
        raise HTTPException(status_code=400, detail="Secret key and value are required")

    manager = get_secrets_vault_manager()
    return await manager.create_secret(user_id, payload)


@router.post("/{secret_id}/reveal", response_model=RevealSecretResponse)
async def reveal_secret(
    secret_id: str,
    user_id: str = Depends(get_user_id),
) -> RevealSecretResponse:
    """Decrypt and reveal plain-text secret value. This action is permanently audit-logged."""
    manager = get_secrets_vault_manager()
    res = await manager.reveal_secret(secret_id, user_id)
    if not res:
        raise HTTPException(status_code=404, detail="Secret not found or access denied")
    return res


@router.put("/{secret_id}", response_model=SecretItem)
async def update_secret(
    secret_id: str,
    payload: UpdateSecretRequest,
    user_id: str = Depends(get_user_id),
) -> SecretItem:
    """Update secret key, environment, rotation interval, or role permissions."""
    manager = get_secrets_vault_manager()
    res = await manager.update_secret(secret_id, user_id, payload)
    if not res:
        raise HTTPException(status_code=404, detail="Secret not found")
    return res


@router.post("/{secret_id}/rotate", response_model=SecretItem)
async def rotate_secret(
    secret_id: str,
    payload: RotateSecretRequest,
    user_id: str = Depends(get_user_id),
) -> SecretItem:
    """Rotate secret: archives old version, stores new encrypted value, bumps version, and resets expiry."""
    manager = get_secrets_vault_manager()
    res = await manager.rotate_secret(secret_id, user_id, payload)
    if not res:
        raise HTTPException(status_code=404, detail="Secret not found")
    return res


@router.delete("/{secret_id}")
async def delete_secret(
    secret_id: str,
    user_id: str = Depends(get_user_id),
) -> dict[str, str]:
    """Permanently delete a secret from the vault."""
    manager = get_secrets_vault_manager()
    success = await manager.delete_secret(secret_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Secret not found")
    return {"message": "Secret deleted successfully"}


@router.get("/audit-logs", response_model=list[SecretAuditLog])
async def list_audit_logs(
    user_id: str = Depends(get_user_id),
) -> list[SecretAuditLog]:
    """Retrieve immutable audit log of secret access, reveals, and rotations."""
    manager = get_secrets_vault_manager()
    return await manager.list_audit_logs(user_id)


@router.get("/team", response_model=list[TeamMember])
async def list_team_members(
    user_id: str = Depends(get_user_id),
) -> list[TeamMember]:
    """List team members who share secret management access."""
    manager = get_secrets_vault_manager()
    return await manager.list_team_members(user_id)


@router.post("/team/invite", response_model=TeamMember, status_code=201)
async def invite_team_member(
    payload: InviteTeamMemberRequest,
    user_id: str = Depends(get_user_id),
) -> TeamMember:
    """Invite team member with specified role permissions."""
    if not payload.email or not payload.name:
        raise HTTPException(status_code=400, detail="Name and email are required")
    manager = get_secrets_vault_manager()
    return await manager.invite_team_member(user_id, payload)


@router.delete("/team/{member_id}")
async def remove_team_member(
    member_id: str,
    user_id: str = Depends(get_user_id),
) -> dict[str, str]:
    """Revoke a team member's secret permissions."""
    manager = get_secrets_vault_manager()
    success = await manager.remove_team_member(user_id, member_id)
    if not success:
        raise HTTPException(status_code=404, detail="Team member not found")
    return {"message": "Team member access revoked"}
