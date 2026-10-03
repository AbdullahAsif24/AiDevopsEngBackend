"""HTTP routes for Custom Domain Management and Health Checks."""
from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_user_id
from ..contracts import (
    CreateDomainRequest,
    CustomDomainItem,
    DomainHealthCheckResponse,
)
from ..services.domains import get_custom_domain_manager

router = APIRouter(prefix="/domains", tags=["domains"])


class ToggleRenewRequest(BaseModel):
    auto_ssl_renew: bool


@router.get("", response_model=list[CustomDomainItem])
async def list_domains(user_id: str = Depends(get_user_id)) -> list[CustomDomainItem]:
    """List all custom domains with DNS, SSL, and health metrics."""
    manager = get_custom_domain_manager()
    return await manager.list_domains(user_id)


@router.post("", response_model=CustomDomainItem, status_code=201)
async def create_domain(
    payload: CreateDomainRequest,
    user_id: str = Depends(get_user_id),
) -> CustomDomainItem:
    """Add a new custom domain for deployment routing."""
    if not payload.domain:
        raise HTTPException(status_code=400, detail="Domain name is required")

    manager = get_custom_domain_manager()
    try:
        return await manager.create_domain(user_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{domain_id}/verify-dns", response_model=CustomDomainItem)
async def verify_dns(
    domain_id: str,
    user_id: str = Depends(get_user_id),
) -> CustomDomainItem:
    """Verify DNS record propagation and auto-trigger SSL issuance."""
    manager = get_custom_domain_manager()
    res = await manager.verify_dns(domain_id, user_id)
    if not res:
        raise HTTPException(status_code=404, detail="Domain not found")
    return res


@router.post("/{domain_id}/issue-ssl", response_model=CustomDomainItem)
async def issue_ssl(
    domain_id: str,
    user_id: str = Depends(get_user_id),
) -> CustomDomainItem:
    """Issue or renew Let's Encrypt SSL certificate for domain."""
    manager = get_custom_domain_manager()
    res = await manager.issue_ssl_certificate(domain_id, user_id)
    if not res:
        raise HTTPException(status_code=404, detail="Domain not found")
    return res


@router.post("/{domain_id}/check-health", response_model=DomainHealthCheckResponse)
async def check_health(
    domain_id: str,
    user_id: str = Depends(get_user_id),
) -> DomainHealthCheckResponse:
    """Probe custom domain endpoint for live latency ms, HTTP status code, and SSL certificate validity."""
    manager = get_custom_domain_manager()
    res = await manager.check_domain_health(domain_id, user_id)
    if not res:
        raise HTTPException(status_code=404, detail="Domain not found")
    return res


@router.patch("/{domain_id}/ssl-renew", response_model=CustomDomainItem)
async def toggle_auto_renew(
    domain_id: str,
    payload: ToggleRenewRequest,
    user_id: str = Depends(get_user_id),
) -> CustomDomainItem:
    """Enable or disable automated SSL certificate renewal."""
    manager = get_custom_domain_manager()
    res = await manager.toggle_auto_renew(domain_id, user_id, payload.auto_ssl_renew)
    if not res:
        raise HTTPException(status_code=404, detail="Domain not found")
    return res


@router.delete("/{domain_id}")
async def delete_domain(
    domain_id: str,
    user_id: str = Depends(get_user_id),
) -> dict[str, str]:
    """Remove a custom domain."""
    manager = get_custom_domain_manager()
    success = await manager.delete_domain(domain_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Domain not found")
    return {"message": "Custom domain removed successfully"}
