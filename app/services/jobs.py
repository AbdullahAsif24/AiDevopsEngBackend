"""In-process job orchestration: store + background worker.

Design for v1 (single-process, concurrency-safe):
  * A dict `_jobs` maps job_id -> JobStatus, mutated only inside an asyncio.Lock
    so concurrent GETs and the background task never clobber each other.
  * `create_job()` immediately returns a job_id and schedules the heavy work as
    an asyncio background task. Nothing heavy (clone/LLM/build) ever blocks the
    request/response cycle.
  * Each job stages through the JobStage enum and pushes JobEvents to the hub.

DevOps wiring:
  * Real docker build+health via docker_build.build_and_test
  * On success, deploy to Render (when credentials exist)
  * Persist jobs / logs / deployments to Supabase when configured
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from ..config import settings
from ..contracts import (
    DockerfileError,
    DockerfileResult,
    JobEvent,
    JobStage,
    JobStatus,
)
from .agent import generate_dockerfile
from .cloner import CloneError, InvalidRepoURL, clone_repo
from .deploy import deploy_configured, deploy_image
from .docker_build import build_and_test
from .events import hub
from .github import InvalidRepoURL as GHInvalidURL
from . import supabase_store as db

# The StatusNotFound sentinel lets callers distinguish "no such job" from a
# genuinely empty record without leaking a sentinel instance.
_JOBS: dict[str, JobStatus] = {}
_jobs_lock = asyncio.Lock()


class JobNotFound(Exception):
    """Raised when GET /jobs/{id} references an unknown id."""


async def _record(job: JobStatus) -> None:
    """Atomically upsert a job record. All writes go through here."""
    async with _jobs_lock:
        _JOBS[job.job_id] = job


def make_job(repo_url: str) -> JobStatus:
    """Create an in-memory JobStatus record (queued) and return it.

    Does NOT schedule the work; the route calls schedule_job() afterwards.
    """
    return JobStatus(
        job_id=uuid.uuid4().hex[:12],
        status=JobStage.QUEUED,
        repo_url=repo_url,
    )


async def schedule_job(job: JobStatus) -> None:
    """Persist the record and spawn the background task.

    We use asyncio.create_task (not BackgroundTasks) so the HTTP response can
    return immediately while the work continues independently of that request's
    lifecycle (BackgroundTasks runs AFTER the response is sent, but is tied to
    the request; a create_task is decoupled and more appropriate for long jobs).
    """
    job.logs.append(_event(job.job_id, JobStage.QUEUED, "Job queued"))
    await _record(job)
    await db.upsert_job(job_id=job.job_id, repo_url=job.repo_url, status=job.status.value)
    await db.append_log(job_id=job.job_id, stage=JobStage.QUEUED.value, message="Job queued")
    asyncio.get_running_loop().create_task(_run_job(job.job_id))


def _event(job_id: str, stage: JobStage, message: str) -> JobEvent:
    return JobEvent(job_id=job_id, stage=stage, message=message)


async def _log(job: JobStatus, stage: JobStage, message: str) -> None:
    """Append an event to the job's log AND broadcast it via the hub."""
    event = _event(job.job_id, stage, message)
    job.logs.append(event)
    job.status = stage
    await _record(job)
    await hub.publish(event)
    await db.append_log(job_id=job.job_id, stage=stage.value, message=message)
    await db.upsert_job(
        job_id=job.job_id,
        repo_url=job.repo_url,
        status=stage.value,
        dockerfile_content=(job.result.dockerfile_content if job.result else None),
        deploy_url=job.deploy_url,
        error=job.error,
    )


async def _run_job(job_id: str) -> None:
    """Execute the full pipeline: clone -> generate/heal -> build -> deploy."""
    async with _jobs_lock:
        job = _JOBS.get(job_id)
    if job is None:
        return

    try:
        await _log(job, JobStage.CLONING, "Cloning repository")
        async with await clone_repo(job.repo_url) as snapshot:
            await _log(job, JobStage.ANALYZING, "Analyzing repository structure")
            await _log(job, JobStage.GENERATING, "Generating Dockerfile via Groq")

            result = await _generate_with_healing(job, snapshot.root)

            if isinstance(result, DockerfileError):
                job.error = result.message + (f": {result.detail}" if result.detail else "")
                await _log(job, JobStage.FAILED, job.error)
                return

            job.result = result
            image_tag = f"aidevops-{job.job_id}:pass"

            if settings.skip_docker_build or not deploy_configured():
                await _deploy_after_build(job, image_tag=image_tag, port=result.port)
            else:
                await _log(job, JobStage.BUILDING, "Build validation passed — starting deploy")
                await _deploy_after_build(job, image_tag=image_tag, port=result.port)

            if job.error:
                await _log(job, JobStage.FAILED, job.error)
                return

            if job.deploy_url:
                msg = f"Deployed successfully: {job.deploy_url}"
            elif settings.skip_docker_build:
                msg = "Dockerfile generated successfully (docker build skipped)"
            elif not deploy_configured():
                msg = "Dockerfile generated and build-tested (deploy skipped — no credentials)"
            else:
                msg = "Build OK but deploy did not return a URL"

            await _log(job, JobStage.DONE, msg)

    except (CloneError, InvalidRepoURL, GHInvalidURL) as exc:
        job.error = f"Clone/validation failed: {exc}"
        await _log(job, JobStage.FAILED, job.error)
    except Exception as exc:  # broad safety net — never let a task die silently
        job.error = f"Unexpected error: {exc}"
        await _log(job, JobStage.FAILED, job.error)


async def _generate_with_healing(
    job: JobStatus, repo_root: str
) -> DockerfileResult | DockerfileError:
    """Run generate_dockerfile with a real docker build_fn for the self-heal loop."""
    loop = asyncio.get_running_loop()
    image_tag = f"aidevops-{job.job_id}:pass"
    port = settings.default_app_port

    # Progress from the sync docker thread → schedule async WebSocket logs.
    def on_progress(message: str) -> None:
        asyncio.run_coroutine_threadsafe(
            _log(job, JobStage.BUILDING, message),
            loop,
        )

    if settings.skip_docker_build:
        def build_fn(dockerfile_content: str) -> Optional[str]:
            return None
    else:
        def build_fn(dockerfile_content: str) -> Optional[str]:
            asyncio.run_coroutine_threadsafe(
                _log(job, JobStage.BUILDING, "Starting Docker build + health check"),
                loop,
            )
            exposed = _guess_port(dockerfile_content) or port
            err = build_and_test(
                repo_root,
                dockerfile_content,
                port=exposed,
                image_tag=image_tag,
                build_timeout_s=settings.docker_build_timeout_s,
                health_timeout_s=settings.docker_health_timeout_s,
                keep_image=True,
                on_progress=on_progress,
            )
            if err:
                asyncio.run_coroutine_threadsafe(
                    _log(job, JobStage.HEALING, f"Build failed — requesting heal: {err[:240]}"),
                    loop,
                )
            return err

    return await generate_dockerfile(
        repo_path=repo_root,
        build_fn=build_fn,
        repo_url=job.repo_url,
        job_id=job.job_id,
    )


def _guess_port(dockerfile_content: str) -> Optional[int]:
    for line in dockerfile_content.splitlines():
        stripped = line.strip().upper()
        if stripped.startswith("EXPOSE"):
            parts = stripped.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return None


async def _deploy_after_build(
    job: JobStatus, *, image_tag: str, port: Optional[int]
) -> None:
    """Push + deploy to Render when configured; always record history when possible."""
    app_port = port or settings.default_app_port

    if settings.skip_docker_build:
        await _log(job, JobStage.BUILDING, "Docker build skipped — deploy skipped too")
        return

    if not deploy_configured():
        await _log(
            job,
            JobStage.BUILDING,
            "Deploy skipped (set RENDER_* and DOCKERHUB_* env vars to enable)",
        )
        return

    loop = asyncio.get_running_loop()

    def on_progress(message: str) -> None:
        asyncio.run_coroutine_threadsafe(
            _log(job, JobStage.BUILDING, message),
            loop,
        )

    result = await loop.run_in_executor(
        None,
        lambda: deploy_image(
            image_tag,
            job_id=job.job_id,
            port=app_port,
            on_progress=on_progress,
        ),
    )

    if result.skipped:
        return

    if not result.ok:
        job.error = f"Deploy failed: {result.error}"
        await db.record_deployment(
            job_id=job.job_id,
            provider=result.provider,
            service_id=result.service_id,
            live_url=None,
            image_tag=result.image_path or image_tag,
            status="failed",
            is_active=False,
        )
        return

    job.deploy_url = result.live_url
    if job.result is not None:
        job.result.metadata["deploy_url"] = result.live_url
        job.result.metadata["deploy_provider"] = result.provider
        job.result.metadata["deploy_service_id"] = result.service_id

    await db.record_deployment(
        job_id=job.job_id,
        provider=result.provider,
        service_id=result.service_id,
        live_url=result.live_url,
        image_tag=result.image_path or image_tag,
        status="live",
        is_active=True,
    )


async def get_job(job_id: str) -> JobStatus:
    """Return the current JobStatus snapshot; raises JobNotFound if unknown."""
    async with _jobs_lock:
        job = _JOBS.get(job_id)
    if job is None:
        raise JobNotFound(job_id)
    return job
