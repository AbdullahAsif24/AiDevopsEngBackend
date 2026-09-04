"""Deploy a successfully built image to the primary free host (Render).

Strategy (hackathon-reliable):
  1. Tag + push the local image to Docker Hub (public).
  2. Create (or reuse) a Render web service that pulls that image.
  3. Poll until a deploy reaches live status, then return the URL.

Critical: Render routes traffic to $PORT (default 10000). We set PORT to the
app's listen port so Express/Flask images on :3000/:8000 actually receive traffic.
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
    # PORT tells Render which container port to route to (and many frameworks
    # also read it). Default Render PORT is 10000 — mismatch causes Not Found.
    payload = {
        "type": "web_service",
        "name": name[:48],
        "ownerId": settings.render_owner_id,
        "image": {
            "ownerId": settings.render_owner_id,
            "imagePath": image_path,
        },
        "envVars": [
            {"key": "PORT", "value": str(port)},
            {"key": "HOST", "value": "0.0.0.0"},
            {"key": "NODE_ENV", "value": "production"},
        ],
        "serviceDetails": {
            "env": "image",
            "runtime": "image",
            "plan": settings.render_plan,
            "region": settings.render_region,
            # "/" is more compatible than /health for Express/static apps.
            "healthCheckPath": "/",
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
            if isinstance(data, list) and data:
                svc = data[0].get("service") or data[0]
            else:
                svc = data.get("service") or data
            return svc, None
    except Exception as exc:
        return None, f"Render API error: {exc}"


def _latest_deploy_status(service_id: str) -> tuple[Optional[str], Optional[str]]:
    """Return (status, error) for the newest deploy on a service."""
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(
                f"{RENDER_API}/services/{service_id}/deploys",
                headers=_render_headers(),
                params={"limit": 1},
            )
            if resp.status_code != 200:
                return None, f"deploys status {resp.status_code}"
            data = resp.json()
            if not data:
                return None, None
            dep = data[0].get("deploy") or data[0]
            return dep.get("status"), None
    except Exception as exc:
        return None, str(exc)


def _service_url(service_id: str) -> Optional[str]:
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(
                f"{RENDER_API}/services/{service_id}",
                headers=_render_headers(),
            )
            if resp.status_code != 200:
                return None
            body = resp.json()
            svc = body.get("service") or body
            url = (
                svc.get("serviceDetails", {}).get("url")
                or svc.get("url")
                or None
            )
            if not url:
                slug = svc.get("slug") or svc.get("name")
                if slug:
                    url = f"https://{slug}.onrender.com"
            if url and not url.startswith("http"):
                url = f"https://{url}"
            return url
    except Exception:
        return None


def _poll_render_url(
    service_id: str,
    timeout_s: float = 480.0,
    on_progress: Optional[Callable[[str], None]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Poll until a deploy is live (not merely until a URL string exists)."""
    deadline = time.monotonic() + timeout_s
    last_err = "waiting for Render deploy"
    terminal_fail = {
        "build_failed",
        "update_failed",
        "canceled",
        "deactivated",
        "pre_deploy_failed",
    }
    terminal_ok = {"live", "available", "update_succeeded", "succeeded"}

    while time.monotonic() < deadline:
        status, err = _latest_deploy_status(service_id)
        url = _service_url(service_id)
        _progress(
            on_progress,
            f"Render deploy status={status or 'unknown'} url={url or '-'}",
        )

        if status and status.lower() in terminal_fail:
            return None, f"Render deploy failed with status={status}"

        if status and status.lower() in terminal_ok and url:
            # Quick HTTP probe — prefer a real response over a bare DNS name.
            try:
                with httpx.Client(timeout=20.0, follow_redirects=True) as client:
                    probe = client.get(url)
                    # 404 from the *app* can still mean routing works; Render edge
                    # "Not Found" plain text usually means not ready yet.
                    body = (probe.text or "")[:80]
                    if probe.status_code == 404 and body.strip().lower() == "not found":
                        last_err = "URL resolves but service still returning edge Not Found"
                    else:
                        return url, None
            except Exception as exc:
                last_err = f"probe failed: {exc}"

        if err:
            last_err = err

        time.sleep(10)

    return None, f"Timed out waiting for Render live deploy ({last_err})"


def fix_service_port(service_id: str, port: int) -> Optional[str]:
    """Update PORT on an existing service and trigger a redeploy. Returns error or None."""
    try:
        with httpx.Client(timeout=60.0) as client:
            # PUT replaces all env vars for the service.
            resp = client.put(
                f"{RENDER_API}/services/{service_id}/env-vars",
                headers=_render_headers(),
                json=[
                    {"key": "PORT", "value": str(port)},
                    {"key": "HOST", "value": "0.0.0.0"},
                    {"key": "NODE_ENV", "value": "production"},
                ],
            )
            if resp.status_code not in (200, 201):
                return f"env-vars update failed ({resp.status_code}): {resp.text[:400]}"

            # Clear overly strict health check if possible via PATCH.
            client.patch(
                f"{RENDER_API}/services/{service_id}",
                headers=_render_headers(),
                json={"serviceDetails": {"healthCheckPath": "/"}},
            )

            dep = client.post(
                f"{RENDER_API}/services/{service_id}/deploys",
                headers=_render_headers(),
                json={"clearCache": "clear"},
            )
            if dep.status_code not in (200, 201, 202):
                return f"redeploy failed ({dep.status_code}): {dep.text[:400]}"
            return None
    except Exception as exc:
        return str(exc)


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
    _progress(on_progress, f"Creating Render service {service_name} (PORT={port})")
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
