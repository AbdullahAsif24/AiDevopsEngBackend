"""Deploy a successfully built image to the primary free host (Render).

Strategy (hackathon-reliable):
  1. Tag + push the local image to Docker Hub (public).
  2. Create (or reuse) a Render web service that pulls that image.
  3. Poll until Render reports a live URL, then return it.

If credentials are missing we skip deploy cleanly so the build/heal demo still
works offline. Fly.io / Alibaba are intentionally not wired in v1.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

from ..config import settings

logger = logging.getLogger("aidevops.deploy")

RENDER_API = "https://api.render.com/v1"


@dataclass
class DeployResult:
    ok: bool
    live_url: Optional[str] = None
    provider: str = "render"
    service_id: Optional[str] = None
    image_path: Optional[str] = None
    error: Optional[str] = None
    skipped: bool = False


def deploy_configured() -> bool:
    """True when enough env is set to attempt a real Render deploy."""
    return bool(
        settings.render_api_key
        and settings.render_owner_id
        and settings.dockerhub_username
        and settings.dockerhub_token
    )


def _progress(cb: Optional[Callable[[str], None]], msg: str) -> None:
    logger.info(msg)
    if cb:
        cb(msg)


def _push_image(
    local_tag: str,
    remote_repo: str,
    on_progress: Optional[Callable[[str], None]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Push local_tag to Docker Hub as remote_repo:tag. Returns (image_path, error)."""
    try:
        import docker
        from docker.errors import DockerException
    except ImportError:
        return None, "docker package not installed"

    try:
        client = docker.from_env()
        client.ping()
    except DockerException as exc:
        return None, f"Docker daemon unavailable: {exc}"

    username = settings.dockerhub_username
    token = settings.dockerhub_token
    tag = local_tag.split(":")[-1] if ":" in local_tag else "latest"
    remote = f"{username}/{remote_repo}:{tag}"

    try:
        _progress(on_progress, f"Tagging image as {remote}")
        image = client.images.get(local_tag)
        image.tag(f"{username}/{remote_repo}", tag=tag)

        _progress(on_progress, "Logging into Docker Hub")
        client.login(username=username, password=token)

        _progress(on_progress, f"Pushing {remote}")
        # push returns a generator of status dicts
        for chunk in client.images.push(
            f"{username}/{remote_repo}", tag=tag, stream=True, decode=True
        ):
            if chunk.get("error"):
                return None, f"Docker push failed: {chunk['error']}"
        return f"docker.io/{remote}", None
    except Exception as exc:
        return None, f"Docker push failed: {exc}"


def _render_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.render_api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _create_render_service(
    name: str,
    image_path: str,
    port: int,
) -> tuple[Optional[dict], Optional[str]]:
    """Create a Render web service from a public Docker image."""
    payload = {
        "type": "web_service",
        "name": name[:48],  # Render name length guard
        "ownerId": settings.render_owner_id,
        "image": {
            "ownerId": settings.render_owner_id,
            "imagePath": image_path,
        },
        "serviceDetails": {
            "env": "image",
            "runtime": "image",
            "plan": settings.render_plan,
            "region": settings.render_region,
            "healthCheckPath": "/health",
            "envSpecificDetails": {
                "dockerCommand": "",
                "dockerContext": "",
                "dockerfilePath": "",
            },
        },
    }

    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{RENDER_API}/services",
                headers=_render_headers(),
                json=payload,
            )
            if resp.status_code not in (200, 201):
                return None, f"Render create failed ({resp.status_code}): {resp.text[:800]}"
            data = resp.json()
            # API wraps as [{ "service": {...} }] or { "service": {...} }
            if isinstance(data, list) and data:
                svc = data[0].get("service") or data[0]
            else:
                svc = data.get("service") or data
            return svc, None
    except Exception as exc:
        return None, f"Render API error: {exc}"


def _poll_render_url(
    service_id: str,
    timeout_s: float = 300.0,
    on_progress: Optional[Callable[[str], None]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Poll Render until the service has a public URL or we time out."""
    deadline = time.monotonic() + timeout_s
    last_err = "waiting for Render service"

    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(
                    f"{RENDER_API}/services/{service_id}",
                    headers=_render_headers(),
                )
                if resp.status_code != 200:
                    last_err = f"Render status {resp.status_code}: {resp.text[:200]}"
                    time.sleep(5)
                    continue

                body = resp.json()
                svc = body.get("service") or body
                # url / serviceDetails.url variants across API versions
                url = (
                    svc.get("serviceDetails", {}).get("url")
                    or svc.get("url")
                    or None
                )
                # Sometimes only the slug is present — compose onrender.com
                if not url:
                    slug = svc.get("slug") or svc.get("name")
                    if slug:
                        url = f"https://{slug}.onrender.com"

                state = (
                    svc.get("serviceDetails", {}).get("state")
                    or svc.get("state")
                    or ""
                )
                _progress(on_progress, f"Render state={state or 'unknown'} url={url or '-'}")

                if url and state.lower() in ("available", "running", "live", ""):
                    # Empty state + url is still useful mid-provision.
                    if state.lower() in ("available", "running", "live") or url:
                        if state.lower() in ("available", "running", "live"):
                            return url if url.startswith("http") else f"https://{url}", None
                        # Have URL but still provisioning — keep waiting a bit
                        if time.monotonic() + 30 > deadline and url:
                            return url if url.startswith("http") else f"https://{url}", None

        except Exception as exc:
            last_err = str(exc)

        time.sleep(8)

    return None, f"Timed out waiting for Render URL ({last_err})"


def deploy_image(
    local_image_tag: str,
    *,
    job_id: str,
    port: int = 8000,
    on_progress: Optional[Callable[[str], None]] = None,
) -> DeployResult:
    """Push local_image_tag and deploy it to Render. Returns DeployResult."""
    if not deploy_configured():
        _progress(
            on_progress,
            "Deploy skipped — set RENDER_API_KEY, RENDER_OWNER_ID, "
            "DOCKERHUB_USERNAME, DOCKERHUB_TOKEN to enable",
        )
        return DeployResult(ok=True, skipped=True, provider="render")

    remote_repo = f"aidevops-{job_id}"
    image_path, err = _push_image(local_image_tag, remote_repo, on_progress)
    if err or not image_path:
        return DeployResult(ok=False, error=err or "push failed", image_path=image_path)

    service_name = f"aidevops-{job_id}"
    _progress(on_progress, f"Creating Render service {service_name}")
    svc, err = _create_render_service(service_name, image_path, port)
    if err or not svc:
        return DeployResult(
            ok=False, error=err or "create failed", image_path=image_path
        )

    service_id = svc.get("id") or svc.get("serviceId")
    if not service_id:
        return DeployResult(
            ok=False,
            error=f"Render response missing service id: {svc!r}",
            image_path=image_path,
        )

    _progress(on_progress, f"Waiting for Render service {service_id} to go live")
    url, err = _poll_render_url(service_id, on_progress=on_progress)
    if err or not url:
        return DeployResult(
            ok=False,
            error=err or "no URL",
            service_id=service_id,
            image_path=image_path,
        )

    return DeployResult(
        ok=True,
        live_url=url,
        service_id=service_id,
        image_path=image_path,
        provider="render",
    )
