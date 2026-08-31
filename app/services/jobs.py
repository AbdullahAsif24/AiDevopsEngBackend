"""In-process job orchestration: store + background worker.

Design for v1 (single-process, concurrency-safe):
  * A dict `_jobs` maps job_id -> JobStatus, mutated only inside an asyncio.Lock
    so concurrent GETs and the background task never clobber each other.
  * `create_job()` immediately returns a job_id and schedules the heavy work as
    an asyncio background task. Nothing heavy (clone/LLM/build) ever blocks the
    request/response cycle.
  * Each job stages through the JobStage enum and pushes JobEvents to the hub.

Concurrency rule: NO shared mutable state crosses jobs. Each task gets its own
RepoSnapshot (temp dir) and its own local variables. `_jobs` only holds records.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from ..contracts import (
    DockerfileError,
    DockerfileResult,
    JobEvent,
    JobStage,
    JobStatus,
)
from .agent import generate_dockerfile
from .cloner import CloneError, InvalidRepoURL, clone_repo
from .events import hub
from .github import InvalidRepoURL as GHInvalidURL

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


async def _run_job(job_id: str) -> None:
    """Execute the full pipeline for a job: clone -> fingerprint -> generate -> heal.

    This runs entirely in the background. On completion (success or failure) we
    set a terminal status and rely on the caller (DevOps) to have provided a
    build_fn wiring if they want the container actually built — otherwise we stop
    at 'generating' with a DockerfileResult.
    """
    # Re-fetch our working record. Only job_id is guaranteed at this point.
    async with _jobs_lock:
        job = _JOBS.get(job_id)
    if job is None:
        return

    try:
        # ---- CLONE ----
        await _log(job, JobStage.CLONING, "Cloning repository")
        # with-block guarantees temp-dir cleanup on success OR failure.
        async with await clone_repo(job.repo_url) as snapshot:
            # ---- ANALYZE (fingerprint) ----
            await _log(job, JobStage.ANALYZING, "Analyzing repository structure")

            # ---- GENERATE (+ self-heal via the caller's build callback) ----
            await _log(job, JobStage.GENERATING, "Generating Dockerfile via Groq")

            # For v1 we don't call docker ourselves; expose generate only.
            # The DevOps engineer injects a `build_fn` to drive the heal loop;
            # without one we stop after generation and mark done with a result.
            result = await _generate_with_healing(job, snapshot.root)

            if isinstance(result, DockerfileError):
                job.error = result.message + (f": {result.detail}" if result.detail else "")
                await _log(job, JobStage.FAILED, job.error)
                return

            job.result = result
            await _log(job, JobStage.DONE, "Dockerfile generated successfully")

    except (CloneError, InvalidRepoURL, GHInvalidURL) as exc:
        await _log(job, JobStage.FAILED, f"Clone/validation failed: {exc}")
    except Exception as exc:  # broad safety net — never let a task die silently
        await _log(job, JobStage.FAILED, f"Unexpected error: {exc}")


async def _generate_with_healing(
    job: JobStatus, repo_root: str
) -> DockerfileResult | DockerfileError:
    """Run generate_dockerfile, wiring an optional build callback.

    The build callback is where the DevOps engineer's docker build would plug in.
    In v1 we leave it as a no-op (generate only) — the API surface accepts a real
    build_fn enabling the bounded self-heal loop that actually builds containers.
    """
    def build_fn(dockerfile_content: str) -> Optional[str]:
        # TODO(DevOps): replace this no-op with a real docker build
        # (docker-py or a subprocess) that returns an error string on failure.
        # Reporting None = "build succeeded", which in our v1 flow stops the
        # pipeline after generation and marks the job done with a result.
        return None

    return await generate_dockerfile(
        repo_path=repo_root,
        build_fn=build_fn,
        repo_url=job.repo_url,
        job_id=job.job_id,
    )


async def get_job(job_id: str) -> JobStatus:
    """Return the current JobStatus snapshot; raises JobNotFound if unknown."""
    async with _jobs_lock:
        job = _JOBS.get(job_id)
    if job is None:
        raise JobNotFound(job_id)
    return job
