"""The core agent: repo path -> Dockerfile, with a bounded self-heal loop.

This is the single clean integration surface for the DevOps engineer:
    generate_dockerfile(repo_path, build_fn) -> DockerfileResult | DockerfileError

Pipeline:
  1. Build the fingerprint from the on-disk repo (never the whole repo).
  2. First Groq pass -> generation prompt -> validated JSON.
  3. Self-heal loop: run the caller's container build; if it errors, patch via
     Groq and rebuild — bounded to MAX_HEAL_RETRIES, then fail with last error.

Concurrency note: this module keeps NO shared mutable state across calls. All
state (fingerprint, current Dockerfile, attempt counter) is local to each call,
so any number of these can run concurrently.
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from pydantic import ValidationError

from ..config import settings
from ..contracts import DockerfileError, DockerfileResult, Framework, RepoFingerprint
from .fingerprint import build_fingerprint
from .groq_client import GroqCallError, _query_groq
from .prompts import build_generation_prompt, build_patch_prompt
from .templates import describe_templates

logger = logging.getLogger("aidevops.agent")

# A build_fn is how we plug in the DevOps engineer's docker build without touching
# docker-py ourselves. It takes the candidate Dockerfile content and returns:
#   * None            -> build succeeded (or builder reports success)
#   * error string    -> build failed with this message
BuildFn = Callable[[str], Optional[str]]


def _coerce_framework(raw: str, fallback: Framework) -> Framework:
    """Map an arbitrary model string onto our Framework enum, with a fallback.
    The model might emit 'nodejs' or 'Node'; we coerce to the enum or fall back
    rather than hard-failing, so a stray label never kills the job.
    """
    try:
        return Framework(raw)
    except ValueError:
        # Accept common aliases; otherwise use the fallback.
        aliases = {
            "nodejs": Framework.NODE, "js": Framework.NODE, "javascript": Framework.NODE,
            "flask": Framework.PYTHON, "fastapi": Framework.PYTHON, "django": Framework.PYTHON,
            "html": Framework.STATIC, "html/css/js": Framework.STATIC, "nginx": Framework.STATIC,
        }
        return aliases.get(raw.lower(), fallback)


def _log(message: str, job_id: Optional[str] = None) -> None:
    """Structured log line; jobs.py turns these into WebSocket JobEvents."""
    logger.info("[%s] %s", job_id or "-", message)


async def _call_and_parse(prompt: str, job_id: Optional[str]) -> DockerfileResult:
    """One Groq call, returning a Pydantic-validated DockerfileResult.

    Throws ValidationError on malformed output so the caller's retry logic can
    treat a bad parse exactly like a failed attempt.
    """
    raw = await _query_groq(prompt)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Not even valid JSON; construct a synthetic ValidationError.
        raise ValidationError("model returned non-JSON", DockerfileResult) from exc

    content = str(data.get("dockerfile_content", "") or "")
    if not content.strip():
        raise ValidationError("dockerfile_content empty", DockerfileResult)

    return DockerfileResult(
        language=str(data.get("language", "") or ""),
        framework=_coerce_framework(str(data.get("framework", "") or ""), Framework.NODE),
        entry_point=data.get("entry_point"),
        port=data.get("port"),
        start_command=data.get("start_command"),
        dockerfile_content=content,
        metadata={"raw_response": data},
    )


async def generate_dockerfile(
    repo_path: str,
    build_fn: BuildFn | None = None,
    repo_url: Optional[str] = None,
    job_id: Optional[str] = None,
) -> DockerfileResult | DockerfileError:
    """Turn a cloned repo into a (hopefully) working Dockerfile, self-healing.

    Args:
        repo_path: absolute filesystem path to the repo root.
        build_fn:   optional callable(dockerfile) -> Optional[build error]. When
                    provided, the self-heal loop actually builds the container and
                    retries on failure. When None, we skip the build step and just
                    return the generated Dockerfile (caller drives building).
        repo_url:   the original GitHub URL (for the fingerprint record + logs).
        job_id:     optional id for log/event tracing.

    Returns:
        DockerfileResult on success; DockerfileError on terminal failure.
    """
    _log("Building fingerprint", job_id)

    # 1. Compact fingerprint — the model only ever sees this, never the repo.
    fingerprint = build_fingerprint(repo_path, repo_url or "")
    skeletons = describe_templates()

    # 2. First generation pass.
    _log("Running Groq generation", job_id)
    prompt = build_generation_prompt(fingerprint, skeletons)
    try:
        current = await _call_and_parse(prompt, job_id)
    except (GroqCallError, ValidationError) as exc:
        logger.warning("Generation failed: %s", exc)
        return DockerfileError(
            message="Failed to generate Dockerfile from Groq",
            stage="generating",
            detail=str(exc),
        )

    # If the caller didn't wire up a build step, hand back the first result.
    if build_fn is None:
        return current

    # 3. Self-healing retry loop (bounded).
    #    Each iteration: run docker build via build_fn; on failure, ask Groq to
    #    patch against the concrete error; rebuild with the patched Dockerfile.
    last_error: Optional[str] = None

    for attempt in range(1, settings.max_heal_retries + 1):
        # Ask the builder how the CURRENT dockerfile fared.
        last_error = build_fn(current.dockerfile_content)

        if not last_error or not last_error.strip():
            _log(f"Build succeeded on attempt (initial+{attempt - 1})", job_id)
            current.metadata["build_attempts"] = attempt
            return current

        _log(f"Build failed (heal attempt {attempt}): {last_error[:200]}", job_id)

        # Patch via Groq against the concrete error string.
        patched = await _patch_once(
            fingerprint, current, last_error, attempt, job_id
        )
        if isinstance(patched, DockerfileError):
            # Patch itself failed to produce — bail out, no point re-iterating.
            return patched
        current = patched

    # Loop exhausted without a clean build.
    return DockerfileError(
        message="Dockerfile still failing after max heal retries",
        stage="healing",
        detail=last_error or "unknown build error",
    )


async def _patch_once(
    fingerprint: RepoFingerprint,
    previous: DockerfileResult,
    build_error: str,
    attempt: int,
    job_id: Optional[str],
) -> DockerfileResult | DockerfileError:
    """Run ONE self-heal patch against a concrete build error."""
    skeletons = describe_templates()
    prompt = build_patch_prompt(
        fingerprint, previous.dockerfile_content, build_error, skeletons, attempt
    )
    try:
        patched = await _call_and_parse(prompt, job_id)
        patched.metadata["healed_attempt"] = attempt
        return patched
    except (GroqCallError, ValidationError) as exc:
        logger.warning("Heal attempt %d failed to parse: %s", attempt, exc)
        return DockerfileError(
            message=f"Heal attempt {attempt} failed",
            stage="healing",
            detail=str(exc),
        )
