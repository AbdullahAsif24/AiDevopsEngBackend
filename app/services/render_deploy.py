"""Deploy backend services to Render from a GitHub repo (real live URLs)."""
from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

import httpx

from ..config import settings
from ..contracts import DeploymentResult


class RenderDeployError(Exception):
    """Raised when Render deployment fails."""


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def sanitize_service_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]", "-", name.lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return (cleaned[:40] or "backend-service")


def to_https(host_or_url: str) -> str:
    value = host_or_url.strip().rstrip(".,);")
    if value.startswith(("http://", "https://")):
        return value
    return f"https://{value}"


def _unwrap(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("service"), dict):
        return payload["service"]
    if isinstance(payload, dict):
        return payload
    return {}


def extract_service_url(service: dict[str, Any]) -> Optional[str]:
    details = service.get("serviceDetails") or {}
    for key in ("url", "deployUrl"):
        value = details.get(key) or service.get(key)
        if value:
            return to_https(str(value))
    slug = service.get("slug") or service.get("name")
    if slug:
        return f"https://{slug}.onrender.com"
    return None


async def deploy_to_render(
    repo_path: str,
    service_name: str,
    dockerfile_content: str,
    access_token: str,
    env_vars: Optional[dict] = None,
    repo_url: Optional[str] = None,
) -> DeploymentResult:
    """Create a Render web service from GitHub and wait until it is live.

    Args:
        repo_path: Path to the repository (for local context)
        service_name: Name for the Render service
        dockerfile_content: Dockerfile content for the service
        access_token: User's Render access token
        env_vars: Optional environment variables to inject
        repo_url: GitHub repository URL (required for Render)

    Returns:
        DeploymentResult with deployment URL and status
    """
    if not access_token:
        raise RenderDeployError("Render access token not provided")
    if not repo_url:
        raise RenderDeployError("GitHub repo URL is required for Render deployment")

    name = sanitize_service_name(service_name)
    git_repo = repo_url.rstrip("/").removesuffix(".git")
    timeout = httpx.Timeout(30.0, read=60.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        owner_id = await _owner_id(client, access_token)
        created = await _create_or_get_service(
            client,
            owner_id=owner_id,
            name=name,
            repo_url=git_repo,
            env_vars=env_vars or {},
            access_token=access_token,
        )
        service_id = created.get("id")
        if not service_id:
            raise RenderDeployError("Render did not return a service id")

        live = await _wait_until_live(client, str(service_id), created, access_token)
        public_url = extract_service_url(live)
        if not public_url:
            raise RenderDeployError("Render service was created but no public URL was returned")

        return DeploymentResult(
            platform="render",
            deployment_url=public_url,
            deployment_id=str(service_id),
            status="deployed",
            message=f"Successfully deployed to Render. Live at {public_url}",
        )


async def _owner_id(client: httpx.AsyncClient, access_token: str) -> str:
    if settings.render_owner_id:
        return settings.render_owner_id

    response = await client.get(
        "https://api.render.com/v1/owners", headers=_headers(access_token)
    )
    if response.status_code != 200:
        raise RenderDeployError(
            f"Failed to list Render owners: {response.status_code} {response.text[:400]}. "
            "Set RENDER_OWNER_ID to your workspace id from Render Settings."
        )
    items = response.json()
    if isinstance(items, list) and items:
        first = items[0]
        owner = first.get("owner") if isinstance(first, dict) else None
        if isinstance(owner, dict) and owner.get("id"):
            return str(owner["id"])
        if isinstance(first, dict) and first.get("id"):
            return str(first["id"])
    raise RenderDeployError(
        "No Render workspace found for this API key. Set RENDER_OWNER_ID."
    )


async def _create_or_get_service(
    client: httpx.AsyncClient,
    owner_id: str,
    name: str,
    repo_url: str,
    env_vars: dict,
    access_token: str,
) -> dict[str, Any]:
    env_payload = [{"key": key, "value": str(value)} for key, value in env_vars.items()]
    body: dict[str, Any] = {
        "type": "web_service",
        "name": name,
        "ownerId": owner_id,
        "repo": repo_url,
        "autoDeploy": "yes",
        "serviceDetails": {
            "runtime": "docker",
            "plan": "starter",
            "region": "oregon",
            "envSpecificDetails": {
                "dockerCommand": "",
                "dockerContext": ".",
                "dockerfilePath": "./Dockerfile",
            },
        },
    }
    if env_payload:
        body["envVars"] = env_payload

    print(f"Creating Render service: {name} from repo: {repo_url}")
    print(f"Environment variables: {len(env_payload)} vars")

    response = await client.post(
        "https://api.render.com/v1/services",
        headers=_headers(access_token),
        json=body,
    )
    if response.status_code in (200, 201):
        print(f"Render service created successfully: {name}")
        return _unwrap(response.json())

    text = response.text
    print(f"Render service creation failed: {response.status_code} - {text[:500]}")
    
    if response.status_code in (409, 400) and "already exists" in text.lower():
        print(f"Service already exists, trying to find existing service: {name}")
        existing = await _find_service_by_name(client, name, access_token)
        if existing:
            print(f"Found existing service: {name}")
            return existing

    raise RenderDeployError(
        f"Failed to create Render service: {response.status_code} {text[:800]}. "
        "Connect this GitHub account in the Render dashboard if the repo cannot be accessed."
    )


async def _find_service_by_name(
    client: httpx.AsyncClient, name: str, access_token: str
) -> Optional[dict[str, Any]]:
    response = await client.get(
        "https://api.render.com/v1/services",
        headers=_headers(access_token),
        params={"name": name, "limit": 20},
    )
    if response.status_code != 200:
        return None
    items = response.json()
    if not isinstance(items, list):
        return None
    for item in items:
        service = item.get("service") if isinstance(item, dict) else None
        if isinstance(service, dict) and service.get("name") == name:
            return service
    return None


async def _wait_until_live(
    client: httpx.AsyncClient,
    service_id: str,
    fallback: dict[str, Any],
    access_token: str,
) -> dict[str, Any]:
    latest_service = fallback
    for _ in range(90):
        service_resp = await client.get(
            f"https://api.render.com/v1/services/{service_id}",
            headers=_headers(access_token),
        )
        if service_resp.status_code == 200:
            latest_service = _unwrap(service_resp.json())

        deploy_resp = await client.get(
            f"https://api.render.com/v1/services/{service_id}/deploys",
            headers=_headers(access_token),
            params={"limit": 1},
        )
        status = ""
        error_details = ""
        if deploy_resp.status_code == 200:
            items = deploy_resp.json()
            if isinstance(items, list) and items:
                first = items[0]
                deploy = first.get("deploy") if isinstance(first, dict) else first
                if isinstance(deploy, dict):
                    status = str(deploy.get("status") or "")
                    # Capture error details if available
                    if deploy.get("error"):
                        error_details = str(deploy.get("error"))
                    if deploy.get("errorMessage"):
                        error_details = str(deploy.get("errorMessage"))
                    if deploy.get("failureDetails"):
                        error_details = str(deploy.get("failureDetails"))

        status_l = status.lower()
        if status_l == "live":
            return latest_service
        if status_l in {"build_failed", "update_failed", "canceled", "deactivated"}:
            error_msg = f"Render deploy ended with status '{status}'"
            if error_details:
                error_msg += f". Details: {error_details}"
            raise RenderDeployError(error_msg)
        await asyncio.sleep(6)

    url = extract_service_url(latest_service)
    if url:
        # First boot on Render can exceed the wait window; still return the real URL.
        return latest_service
    raise RenderDeployError("Timed out waiting for Render to finish deploying")


async def get_deployment_status(service_id: str, access_token: str) -> dict:
    if not access_token:
        raise RenderDeployError("Render access token not provided")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"https://api.render.com/v1/services/{service_id}",
            headers=_headers(access_token),
        )
        if response.status_code != 200:
            raise RenderDeployError(f"Failed to get Render service status: {response.text}")
        return response.json()
