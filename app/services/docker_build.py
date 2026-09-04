"""Docker build-and-test step owned by the DevOps / Infra engineer.

Contract for the AI/Backend self-heal loop:
  build_and_test(repo_path, dockerfile_content, ...) -> Optional[str]
    * None            -> image built AND container passed the health check
    * clean error str -> failure reason fed back into Groq (not a raw dump)

Timeouts are bounded so one bad Dockerfile cannot hang the demo.
"""
from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from typing import Callable, Optional

logger = logging.getLogger("aidevops.docker_build")

DEFAULT_BUILD_TIMEOUT_S = 180
DEFAULT_HEALTH_TIMEOUT_S = 45
DEFAULT_HEALTH_INTERVAL_S = 1.5
HEALTH_PATHS = ("/health", "/api/health", "/", "/ping")


class DockerUnavailable(Exception):
    """Docker daemon is not reachable (Desktop not running, no socket, etc.)."""


def _get_client(timeout_s: float = DEFAULT_BUILD_TIMEOUT_S):
    """Lazy-import docker SDK so the API still boots without Docker installed."""
    try:
        import docker
        from docker.errors import DockerException
    except ImportError as exc:
        raise DockerUnavailable(
            "docker package not installed — pip install docker"
        ) from exc

    try:
        client = docker.from_env(timeout=int(timeout_s))
        client.ping()
        return client
    except DockerException as exc:
        raise DockerUnavailable(f"Docker daemon unavailable: {exc}") from exc


def _clean_error(prefix: str, detail: str, limit: int = 1200) -> str:
    """Turn noisy build/run output into a readable string for the heal loop."""
    text = (detail or "").strip()
    if len(text) > limit:
        text = "…\n" + text[-limit:]
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    compact = "\n".join(lines[-40:])
    return f"{prefix}: {compact}" if compact else prefix


def _write_dockerfile(repo_path: str, content: str) -> str:
    path = os.path.join(repo_path, "Dockerfile")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content if content.endswith("\n") else content + "\n")
    return path


def _pick_free_host_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _tcp_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _http_ok(host: str, port: int, path: str) -> bool:
    try:
        import urllib.request

        url = f"http://{host}:{port}{path}"
        with urllib.request.urlopen(url, timeout=2.0) as resp:
            return 200 <= getattr(resp, "status", 200) < 500
    except Exception:
        return False


def _health_check(
    host: str,
    port: int,
    timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S,
    interval_s: float = DEFAULT_HEALTH_INTERVAL_S,
) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout_s
    last_note = "waiting for port"

    while time.monotonic() < deadline:
        if not _tcp_ready(host, port):
            last_note = f"port {port} not accepting connections yet"
            time.sleep(interval_s)
            continue

        for path in HEALTH_PATHS:
            if _http_ok(host, port, path):
                return True, f"HTTP {path} responded on :{port}"

        return True, f"TCP :{port} open (no HTTP health path matched)"

    return False, last_note


def build_and_test(
    repo_path: str,
    dockerfile_content: str,
    *,
    port: int = 8000,
    image_tag: Optional[str] = None,
    build_timeout_s: float = DEFAULT_BUILD_TIMEOUT_S,
    health_timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S,
    keep_image: bool = True,
    buildargs: Optional[dict[str, str]] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """Build an image from dockerfile_content and confirm the app boots.

    Returns:
        None on success, or a clean error string on failure.
    """
    def progress(msg: str) -> None:
        logger.info(msg)
        if on_progress:
            on_progress(msg)

    if not dockerfile_content or not dockerfile_content.strip():
        return "Build failed: empty Dockerfile content"

    if not os.path.isdir(repo_path):
        return f"Build failed: repo path does not exist: {repo_path}"

    try:
        client = _get_client(build_timeout_s)
    except DockerUnavailable as exc:
        return f"Build failed: {exc}"

    from docker.errors import BuildError, APIError, ContainerError, ImageNotFound

    tag = image_tag or f"aidevops-local:{uuid.uuid4().hex[:10]}"
    container = None
    host_port = _pick_free_host_port()

    try:
        _write_dockerfile(repo_path, dockerfile_content)
        # SPA template COPY nginx.spa.conf — ensure it exists in the build context.
        if "nginx.spa.conf" in dockerfile_content:
            from .templates import ensure_nginx_spa_conf

            ensure_nginx_spa_conf(repo_path)

        progress(f"Building image {tag}")

        build_logs: list[str] = []
        try:
            _, log_gen = client.images.build(
                path=repo_path,
                dockerfile="Dockerfile",
                tag=tag,
                rm=True,
                forcerm=True,
                buildargs=buildargs or {},
            )
            for chunk in log_gen:
                line = chunk.get("stream") or chunk.get("status") or chunk.get("error") or ""
                if line:
                    build_logs.append(str(line).rstrip())
                if chunk.get("error"):
                    return _clean_error("Docker build failed", "\n".join(build_logs) or str(chunk["error"]))
        except BuildError as exc:
            log_text = ""
            try:
                log_text = "\n".join(
                    str(c.get("stream") or c.get("error") or c)
                    for c in (exc.build_log or [])
                )
            except Exception:
                log_text = str(exc)
            return _clean_error("Docker build failed", log_text or str(exc))
        except (APIError, OSError) as exc:
            return _clean_error("Docker build failed", "\n".join(build_logs) or str(exc))

        progress(f"Image built; starting container on host port {host_port}")

        try:
            container = client.containers.run(
                tag,
                detach=True,
                ports={f"{port}/tcp": ("127.0.0.1", host_port)},
                auto_remove=False,
                name=f"aidevops-{uuid.uuid4().hex[:8]}",
            )
        except (APIError, ContainerError, ImageNotFound) as exc:
            return _clean_error("Container start failed", str(exc))

        time.sleep(1.0)
        container.reload()
        if container.status not in ("created", "running"):
            logs = ""
            try:
                logs = container.logs(tail=80).decode("utf-8", errors="replace")
            except Exception:
                pass
            return _clean_error(
                f"Container exited early (status={container.status})",
                logs or "no logs",
            )

        ok, note = _health_check("127.0.0.1", host_port, timeout_s=health_timeout_s)
        if not ok:
            logs = ""
            try:
                logs = container.logs(tail=80).decode("utf-8", errors="replace")
            except Exception:
                pass
            return _clean_error(f"Health check failed ({note})", logs)

        progress(f"Health check passed: {note}")
        return None

    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                logger.debug("Failed to remove container", exc_info=True)
        if not keep_image:
            try:
                client.images.remove(tag, force=True)
            except Exception:
                logger.debug("Failed to remove image %s", tag, exc_info=True)


def make_build_fn(
    repo_path: str,
    *,
    port: int = 8000,
    image_tag: Optional[str] = None,
    build_timeout_s: float = DEFAULT_BUILD_TIMEOUT_S,
    health_timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S,
    buildargs: Optional[dict[str, str]] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Callable[[str], Optional[str]]:
    """Return a sync build_fn(dockerfile_content) -> Optional[error] for agent.py."""

    def _build_fn(dockerfile_content: str) -> Optional[str]:
        return build_and_test(
            repo_path,
            dockerfile_content,
            port=port,
            image_tag=image_tag,
            build_timeout_s=build_timeout_s,
            health_timeout_s=health_timeout_s,
            keep_image=True,
            buildargs=buildargs,
            on_progress=on_progress,
        )

    return _build_fn
