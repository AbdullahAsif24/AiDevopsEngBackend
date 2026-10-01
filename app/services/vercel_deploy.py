"""Deploy frontend apps to Vercel from source files (real production URLs)."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Optional

import httpx

from ..config import settings
from ..contracts import DeploymentResult


class VercelDeployError(Exception):
    """Raised when Vercel deployment fails."""


_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".next",
    "dist",
    "build",
    "coverage",
    ".venv",
    "venv",
    "__pycache__",
    ".turbo",
    ".output",
    ".cache",
    ".idea",
    ".vscode",
    ".pnpm-store",
}
_SKIP_FILES = {".DS_Store", "Thumbs.db", ".env", ".env.local", ".env.production"}
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_FILES = 4000


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _team_params() -> dict[str, str]:
    if settings.vercel_team_id:
        return {"teamId": settings.vercel_team_id}
    return {}


def sanitize_project_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9._-]", "-", name.lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-.")
    return (cleaned[:100] or "frontend-app")


def vercel_framework_slug(framework: str) -> Optional[str]:
    text = (framework or "").lower()
    mapping = (
        ("next", "nextjs"),
        ("nuxt", "nuxtjs"),
        ("svelte", "sveltekit-1"),
        ("remix", "remix"),
        ("astro", "astro"),
        ("gatsby", "gatsby"),
        ("angular", "angular"),
        ("vue", "vue"),
        ("vite", "vite"),
        ("create-react-app", "create-react-app"),
        ("cra", "create-react-app"),
        ("react", "vite"),
    )
    for needle, slug in mapping:
        if needle in text:
            return slug
    return None


def collect_source_files(repo_path: str) -> list[tuple[str, bytes, str]]:
    root = Path(repo_path)
    collected: list[tuple[str, bytes, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if fname in _SKIP_FILES:
                continue
            full = Path(dirpath) / fname
            rel = full.relative_to(root).as_posix()
            try:
                if full.stat().st_size > _MAX_FILE_BYTES:
                    continue
                data = full.read_bytes()
            except OSError:
                continue
            collected.append((rel, data, hashlib.sha1(data).hexdigest()))
            if len(collected) >= _MAX_FILES:
                return collected
    return collected


def to_https(host_or_url: str) -> str:
    value = host_or_url.strip().rstrip(".,);")
    if value.startswith(("http://", "https://")):
        return value
    return f"https://{value}"


def pick_public_url(deployment: dict[str, Any]) -> str:
    aliases = deployment.get("alias") or deployment.get("aliases") or []
    candidates: list[str] = []
    if isinstance(aliases, list):
        candidates.extend(str(item) for item in aliases if item)
    url = deployment.get("url") or ""
    if url:
        candidates.append(str(url))

    cleaned: list[str] = []
    for candidate in candidates:
        if "vercel.com/" in candidate and ".vercel.app" not in candidate:
            continue
        cleaned.append(to_https(candidate))
    if not cleaned:
        raise VercelDeployError("Vercel returned no public deployment URL")

    production = [
        item
        for item in cleaned
        if ".vercel.app" in item and "--" not in item.split("://", 1)[-1]
    ]
    return production[0] if production else cleaned[0]


async def deploy_to_vercel(
    repo_path: str,
    project_name: str,
    framework: str,
    access_token: str,
    env_vars: dict[str, str] | None = None,
) -> DeploymentResult:
    """Upload source to Vercel, wait until READY, return the live https URL.

    Args:
        repo_path: Path to the repository to deploy
        project_name: Name for the Vercel project
        framework: Detected framework type
        access_token: User's Vercel access token
        env_vars: Optional environment variables to inject

    Returns:
        DeploymentResult with deployment URL and status
    """
    if not access_token:
        raise VercelDeployError("Vercel access token not provided")

    name = sanitize_project_name(project_name)
    files = collect_source_files(repo_path)
    if not files:
        raise VercelDeployError("No source files found to deploy")

    timeout = httpx.Timeout(30.0, read=180.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        file_refs = await _upload_files(client, files, access_token)
        deployment = await _create_deployment(
            client, name, framework, file_refs, access_token, env_vars
        )
        deployment_id = deployment.get("id") or deployment.get("uid")
        if not deployment_id:
            raise VercelDeployError("Vercel did not return a deployment id")

        ready = await _wait_until_ready(client, str(deployment_id), access_token)
        public_url = pick_public_url(ready)
        return DeploymentResult(
            platform="vercel",
            deployment_url=public_url,
            deployment_id=str(deployment_id),
            status="deployed",
            message=f"Successfully deployed to Vercel. Live at {public_url}",
        )


async def _upload_files(
    client: httpx.AsyncClient,
    files: list[tuple[str, bytes, str]],
    access_token: str,
) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(6)

    async def upload(rel: str, data: bytes, sha: str) -> dict[str, Any]:
        async with sem:
            response = await client.post(
                "https://api.vercel.com/v2/files",
                params=_team_params(),
                headers={
                    **_auth_headers(access_token),
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(data)),
                    "x-vercel-digest": sha,
                },
                content=data,
            )
            if response.status_code not in (200, 201):
                raise VercelDeployError(
                    f"Failed to upload {rel}: {response.status_code} {response.text[:400]}"
                )
            return {"file": rel, "sha": sha, "size": len(data)}

    return list(await asyncio.gather(*[upload(*item) for item in files]))


async def _create_deployment(
    client: httpx.AsyncClient,
    name: str,
    framework: str,
    file_refs: list[dict[str, Any]],
    access_token: str,
    env_vars: dict[str, str] | None = None,
) -> dict[str, Any]:
    slug = vercel_framework_slug(framework)
    project_settings: dict[str, Any] = {}
    if slug:
        project_settings["framework"] = slug

    body: dict[str, Any] = {
        "name": name,
        "project": name,
        "target": "production",
        "files": file_refs,
        "projectSettings": project_settings,
    }

    # Add environment variables if provided
    if env_vars:
        body["env"] = [
            {"key": key, "value": value, "type": "encrypted"}
            for key, value in env_vars.items()
        ]

    params = {
        **_team_params(),
        "skipAutoDetectionConfirmation": 1,
        "forceNew": 1,
    }
    response = await client.post(
        "https://api.vercel.com/v13/deployments",
        params=params,
        headers={**_auth_headers(access_token), "Content-Type": "application/json"},
        json=body,
    )
    if response.status_code not in (200, 201):
        raise VercelDeployError(
            f"Vercel create deployment failed: {response.status_code} {response.text[:800]}"
        )
    return response.json()


async def _wait_until_ready(
    client: httpx.AsyncClient, deployment_id: str, access_token: str
) -> dict[str, Any]:
    for _ in range(90):
        response = await client.get(
            f"https://api.vercel.com/v13/deployments/{deployment_id}",
            params=_team_params(),
            headers=_auth_headers(access_token),
        )
        if response.status_code != 200:
            raise VercelDeployError(
                f"Failed to poll Vercel deployment: {response.status_code} {response.text[:400]}"
            )
        payload = response.json()
        state = str(payload.get("readyState") or payload.get("status") or "").upper()
        if state == "READY":
            return payload
        if state in {"ERROR", "CANCELED", "CANCELLED"}:
            detail = payload.get("errorMessage") or payload.get("error") or state
            raise VercelDeployError(f"Vercel build failed: {detail}")
        await asyncio.sleep(4)
    raise VercelDeployError("Timed out waiting for Vercel to finish building")
