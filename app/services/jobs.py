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
import json
import uuid
from typing import Optional

from ..contracts import (
    DeploymentType,
    DetectionResult,
    DockerfileError,
    DockerfileResult,
    JobDetection,
    JobEvent,
    JobStage,
    JobStatus,
)
from .agent import generate_dockerfile
from .cloner import CloneError, InvalidRepoURL, clone_repo
from .deployment_detector import detect_deployment_type
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
            job.repo_path = snapshot.root
            await _record(job)

            # ---- DETECT (deployment type) ----
            # Runs immediately after clone, before any Dockerfile generation.
            # Branches on detection result (see the switch below).
            await _log(job, JobStage.ANALYZING, "Detecting deployment type")
            detection = await validate_detection(job, snapshot.root)
            if detection is None:
                return  # job already marked failed/needs_review by validate_detection

            # ---- ANALYZE (fingerprint) / Dockerfile branch ----
            if detection.needs_dockerfile:
                # Container path: generate a Dockerfile.
                await _log(job, JobStage.GENERATING, "Generating Dockerfile via Groq")

                # For v1 we don't call docker ourselves; expose generate only.
                result = await _generate_with_healing(job, snapshot.root)

                if isinstance(result, DockerfileError):
                    job.error = result.message + (f": {result.detail}" if result.detail else "")
                    await _log(job, JobStage.FAILED, job.error)
                    return

                job.result = result
                await _log(job, JobStage.DONE, "Dockerfile generated successfully")
            elif detection.deployment_type in (DeploymentType.STATIC, DeploymentType.VERCEL_NATIVE):
                # Static / Vercel-native path: no Dockerfile, deploy straight to Vercel.
                await _log(
                    job,
                    JobStage.DONE,
                    f"{detection.detected_framework}: no Dockerfile needed, deploying to Vercel",
                )
            else:
                # AMBIGUOUS handled inside validate_detection (job paused for review).
                pass

    except (CloneError, InvalidRepoURL, GHInvalidURL) as exc:
        await _log(job, JobStage.FAILED, f"Clone/validation failed: {exc}")
    except Exception as exc:  # broad safety net — never let a task die silently
        await _log(job, JobStage.FAILED, f"Unexpected error: {exc}")


async def validate_detection(job: JobStatus, repo_path: str) -> Optional[DetectionResult]:
    """Run deployment detection for a job and persist + broadcast the result.

    * Persists a JobDetection onto the job and emits a
      {"step": "detection", "status": "complete", "result": {...}} WebSocket
      event so the dashboard shows it live.
    * For AMBIGUOUS results, flips the job into NEEDS_REVIEW and pauses the
      pipeline until the user calls POST /jobs/{id}/override-detection.
    * Returns the DetectionResult, or None if the pipeline should stop (paused
      for review or detection failed hard).

    The whole detection call is wrapped so a bad repo can never kill the job.
    """
    try:
        result = await detect_deployment_type(repo_path)
    except Exception as exc:  # broad safety net — never let detection kill the job
        result = DetectionResult(
            deployment_type=DeploymentType.AMBIGUOUS,
            confidence="low",
            detected_framework="unknown",
            reasoning="Deployment detection failed",
            needs_dockerfile=False,
            detection_method="rule_based",
            ambiguous_reason=f"Detection service unavailable, manual classification required: {exc}",
        )

    # Persist a flat snapshot of the detection on the job record.
    job.detection = JobDetection(
        deployment_type=result.deployment_type,
        needs_dockerfile=result.needs_dockerfile,
        detected_framework=result.detected_framework,
        entry_point=result.entry_point,
        listen_port=result.listen_port,
        reasoning=result.reasoning,
        detection_method=result.detection_method,
    )
    job.status = (
        JobStage.NEEDS_REVIEW
        if result.deployment_type == DeploymentType.AMBIGUOUS
        else job.status
    )
    await _record(job)

    # Emit the structured detection event (stable dashboard payload).
    await hub.publish(_detection_event(job.job_id, result))

    # If ambiguous, pause the pipeline for manual review.
    if result.deployment_type == DeploymentType.AMBIGUOUS:
        reason = result.ambiguous_reason or result.reasoning or "unknown"
        await _log(
            job,
            JobStage.NEEDS_REVIEW,
            f"Deployment type ambiguous ({reason}). "
            "Waiting for manual override via POST /jobs/{id}/override-detection.",
        )
        return None

    return result


def _detection_event(job_id: str, result: DetectionResult) -> JobEvent:
    """Wrap a DetectionResult into the event hub's {step,status,result} payload."""
    message = json.dumps(
        {
            "step": "detection",
            "status": "complete",
            "result": result.model_dump(),
        }
    )
    return JobEvent(
        job_id=job_id,
        stage=JobStage.ANALYZING,
        message=message,
    )


async def override_detection(job_id: str, deployment_type: DeploymentType) -> DetectionResult:
    """Manually confirm a deployment type for an AMBIGUOUS job.

    Persists the override and marks the job ready to resume so the pipeline can
    continue. Returns a DetectionResult reflecting the manual classification.

    Raises JobNotFound if the job doesn't exist.
    """
    async with _jobs_lock:
        job = _JOBS.get(job_id)
    if job is None:
        raise JobNotFound(job_id)

    framework = job.detection.detected_framework if job.detection else "unknown"
    entry_point = job.detection.entry_point if job.detection else None
    listen_port = job.detection.listen_port if job.detection else None
    method = job.detection.detection_method if job.detection else "rule_based"

    result = DetectionResult(
        deployment_type=deployment_type,
        confidence="high",
        detected_framework=framework,
        entry_point=entry_point,
        listen_port=listen_port,
        reasoning="Manually confirmed by user via override endpoint.",
        needs_dockerfile=deployment_type == DeploymentType.CONTAINER,
        detection_method=method,
    )

    job.detection = JobDetection(
        deployment_type=deployment_type,
        needs_dockerfile=result.needs_dockerfile,
        detected_framework=framework,
        entry_point=entry_point,
        listen_port=listen_port,
        reasoning=result.reasoning,
        detection_method=method,
    )
    job.status = JobStage.QUEUED  # unblock the paused pipeline
    await _record(job)
    return result


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


async def list_jobs() -> list[JobStatus]:
    """Return all jobs, most recently created first."""
    async with _jobs_lock:
        jobs = list(_JOBS.values())
    jobs.sort(key=lambda j: j.created_at, reverse=True)
    return jobs
