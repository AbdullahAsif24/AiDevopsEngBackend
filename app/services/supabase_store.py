"""Supabase persistence for jobs, deployment history, and log summaries.

Uses PostgREST over httpx so we stay dependency-light. When SUPABASE_URL /
SUPABASE_SERVICE_KEY are unset, every call becomes a no-op success — the
in-memory job store remains the source of truth for the live demo.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ..config import settings

logger = logging.getLogger("aidevops.supabase")


def supabase_configured() -> bool:
    return bool(settings.supabase_url and settings.supabase_service_key)


def _headers() -> dict[str, str]:
    return {
        "apikey": settings.supabase_service_key,
        "Authorization": f"Bearer {settings.supabase_service_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _rest(path: str) -> str:
    base = settings.supabase_url.rstrip("/")
    return f"{base}/rest/v1/{path.lstrip('/')}"


async def upsert_job(
    *,
    job_id: str,
    repo_url: str,
    status: str,
    dockerfile_content: Optional[str] = None,
    deploy_url: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    if not supabase_configured():
        return

    row = {
        "id": job_id,
        "repo_url": repo_url,
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "dockerfile_content": dockerfile_content,
        "deploy_url": deploy_url,
        "error": error,
    }
    # Drop Nones so we don't overwrite existing columns with null on partial updates
    # for fields we didn't intend to clear — keep explicit nulls for error clear.
    payload = {k: v for k, v in row.items() if v is not None or k in ("error",)}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                _rest("jobs"),
                headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                params={"on_conflict": "id"},
                json=payload,
            )
            if resp.status_code >= 400:
                logger.warning("supabase upsert_job failed: %s %s", resp.status_code, resp.text[:300])
    except Exception:
        logger.warning("supabase upsert_job error", exc_info=True)


async def append_log(
    *,
    job_id: str,
    stage: str,
    message: str,
) -> None:
    if not supabase_configured():
        return

    row = {
        "job_id": job_id,
        "stage": stage,
        "message": message[:4000],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _rest("logs"),
                headers={**_headers(), "Prefer": "return=minimal"},
                json=row,
            )
            if resp.status_code >= 400:
                logger.warning("supabase append_log failed: %s", resp.status_code)
    except Exception:
        logger.warning("supabase append_log error", exc_info=True)


async def record_deployment(
    *,
    job_id: str,
    provider: str,
    service_id: Optional[str],
    live_url: Optional[str],
    image_tag: Optional[str],
    status: str,
    is_active: bool = True,
) -> Optional[dict[str, Any]]:
    if not supabase_configured():
        return None

    # Deactivate previous active deployments for this job when activating a new one.
    if is_active:
        await _deactivate_job_deployments(job_id)

    row = {
        "job_id": job_id,
        "provider": provider,
        "service_id": service_id,
        "live_url": live_url,
        "image_tag": image_tag,
        "status": status,
        "is_active": is_active,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                _rest("deployments"),
                headers=_headers(),
                json=row,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "supabase record_deployment failed: %s %s",
                    resp.status_code,
                    resp.text[:300],
                )
                return None
            data = resp.json()
            return data[0] if isinstance(data, list) and data else data
    except Exception:
        logger.warning("supabase record_deployment error", exc_info=True)
        return None


async def _deactivate_job_deployments(job_id: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.patch(
                _rest("deployments"),
                headers=_headers(),
                params={"job_id": f"eq.{job_id}", "is_active": "eq.true"},
                json={"is_active": False},
            )
    except Exception:
        logger.warning("supabase deactivate deployments error", exc_info=True)


async def list_deployments(
    *,
    job_id: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if not supabase_configured():
        return []

    params: dict[str, str] = {
        "select": "*",
        "order": "created_at.desc",
        "limit": str(limit),
    }
    if job_id:
        params["job_id"] = f"eq.{job_id}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                _rest("deployments"),
                headers=_headers(),
                params=params,
            )
            if resp.status_code >= 400:
                logger.warning("supabase list_deployments failed: %s", resp.status_code)
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception:
        logger.warning("supabase list_deployments error", exc_info=True)
        return []


async def get_deployment(deployment_id: str) -> Optional[dict[str, Any]]:
    if not supabase_configured():
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _rest("deployments"),
                headers=_headers(),
                params={"id": f"eq.{deployment_id}", "select": "*", "limit": "1"},
            )
            if resp.status_code >= 400:
                return None
            data = resp.json()
            return data[0] if isinstance(data, list) and data else None
    except Exception:
        logger.warning("supabase get_deployment error", exc_info=True)
        return None


async def activate_deployment(deployment_id: str) -> Optional[dict[str, Any]]:
    """Basic rollback: mark this historical deployment as the active one.

    Full traffic switch on Render would require updating the service image to
    this row's image_tag — we attempt that when RENDER credentials exist.
    """
    row = await get_deployment(deployment_id)
    if not row:
        return None

    job_id = row.get("job_id")
    if job_id:
        await _deactivate_job_deployments(job_id)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.patch(
                _rest("deployments"),
                headers=_headers(),
                params={"id": f"eq.{deployment_id}"},
                json={"is_active": True, "status": "rolled_back_active"},
            )
            if resp.status_code >= 400:
                return None
            data = resp.json()
            activated = data[0] if isinstance(data, list) and data else row
    except Exception:
        logger.warning("supabase activate_deployment error", exc_info=True)
        return None

    # Best-effort: point the Render service back at this image tag.
    service_id = activated.get("service_id")
    image_tag = activated.get("image_tag")
    if service_id and image_tag and settings.render_api_key:
        await _render_update_image(service_id, image_tag)

    return activated


async def _render_update_image(service_id: str, image_path: str) -> None:
    """Ask Render to redeploy an existing service from a previous image."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Trigger a deploy; exact shape varies — image update via PATCH.
            await client.post(
                f"https://api.render.com/v1/services/{service_id}/deploys",
                headers={
                    "Authorization": f"Bearer {settings.render_api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={"imageUrl": image_path},
            )
    except Exception:
        logger.warning("render rollback redeploy failed", exc_info=True)
