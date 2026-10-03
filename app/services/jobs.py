"""In-process job orchestration: store + background worker.

Design for v2 (database-backed, multi-user):
  * Database-backed job storage via Supabase instead of in-memory dict
  * User-specific job isolation via user_id foreign key
  * `create_job()` immediately returns a job_id and schedules the heavy work as
    an asyncio background task. Nothing heavy (clone/LLM/build) ever blocks the
    request/response cycle.
  * Each job stages through the JobStage enum and pushes JobEvents to the hub.

Concurrency rule: NO shared mutable state crosses jobs. Each task gets its own
RepoSnapshot (temp dir) and its own local variables. Database holds records.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import uuid
from typing import Optional

from ..contracts import (
    DeploymentType,
    DeploymentResult,
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
from .github import InvalidRepoURL as GHInvalidURL, parse_github_url
from .vercel_deploy import VercelDeployError, deploy_to_vercel
from .render_deploy import RenderDeployError, deploy_to_render
from .pulumi_deploy import PulumiDeployError, get_pulumi_deployer
from .database import get_db_service
from .oauth import get_vercel_oauth, get_render_oauth, get_railway_oauth
from .github_auth import get_user_github_token
from ..config import settings


class JobNotFound(Exception):
    """Raised when GET /jobs/{id} references an unknown id."""


async def _record(job: JobStatus) -> None:
    """Atomically upsert a job record in the database. All writes go through here."""
    db = get_db_service()
    # Convert JobStatus to dict for database storage
    job_dict = {
        "status": job.status.value,
        "result": job.result.model_dump() if job.result else None,
        "error": job.error,
        "detection": job.detection.model_dump() if job.detection else None,
        "deployment": job.deployment.model_dump() if job.deployment else None,
        "repo_path": job.repo_path,
    }
    await db.update_job(job.job_id, job_dict)


def make_job(user_id: str, repo_url: str) -> JobStatus:
    """Create a JobStatus record (queued) and return it.

    Does NOT schedule the work; the route calls schedule_job() afterwards.

    Args:
        user_id: The user ID creating the job
        repo_url: The repository URL to deploy

    Returns:
        JobStatus with queued status
    """
    return JobStatus(
        job_id=uuid.uuid4().hex[:12],
        status=JobStage.QUEUED,
        repo_url=repo_url,
    )


async def schedule_job(job: JobStatus, user_id: str) -> None:
    """Persist the record in database and spawn the background task.

    We use asyncio.create_task (not BackgroundTasks) so the HTTP response can
    return immediately while the work continues independently of that request's
    lifecycle (BackgroundTasks runs AFTER the response is sent, but is tied to
    the request; a create_task is decoupled and more appropriate for long jobs).

    Args:
        job: The job to schedule
        user_id: The user ID creating the job
    """
    db = get_db_service()

    # Create job in database
    await db.create_job(
        user_id=user_id,
        job_id=job.job_id,
        repo_url=job.repo_url,
        status=job.status.value,
    )

    # Add initial log event
    event = _event(job.job_id, JobStage.QUEUED, "Job queued")
    await db.add_job_log(job.job_id, event)
    await hub.publish(event)

    # Spawn background task
    asyncio.get_running_loop().create_task(_run_job(job.job_id, user_id))


def _event(job_id: str, stage: JobStage, message: str) -> JobEvent:
    return JobEvent(job_id=job_id, stage=stage, message=message)


async def _log(job_id: str, stage: JobStage, message: str) -> None:
    """Append an event to the job's log in database AND broadcast it via the hub."""
    db = get_db_service()
    event = _event(job_id, stage, message)
    await db.add_job_log(job_id, event)
    await db.update_job_status(job_id, stage.value)
    await hub.publish(event)


async def _run_job(job_id: str, user_id: str) -> None:
    """Execute the full pipeline for a job: clone -> fingerprint -> generate -> heal.

    This runs entirely in the background. On completion (success or failure) we
    set a terminal status in the database.

    Args:
        job_id: The job identifier
        user_id: The user ID who created the job
    """
    db = get_db_service()

    # Re-fetch our working record from database. Only job_id is guaranteed at this point.
    job_record = await db.get_job(job_id)
    if job_record is None:
        return

    # Reconstruct JobStatus from database record
    job = JobStatus(
        job_id=job_record["job_id"],
        status=JobStage(job_record["status"]),
        repo_url=job_record["repo_url"],
        created_at=job_record["created_at"],
        logs=[],  # Logs loaded separately if needed
        result=job_record.get("result"),
        error=job_record.get("error"),
        detection=job_record.get("detection"),
        deployment=job_record.get("deployment"),
        repo_path=job_record.get("repo_path"),
    )

    try:
        # ---- CLONE ----
        await _log(job_id, JobStage.CLONING, "Cloning repository")

        # Get user's GitHub token for private repo access
        github_token = await get_user_github_token(user_id)

        # with-block guarantees temp-dir cleanup on success OR failure.
        async with await clone_repo(job.repo_url, github_token=github_token) as snapshot:
            await db.update_job_repo_path(job_id, snapshot.root)

            # ---- DETECT (deployment type) ----
            # Runs immediately after clone, before any Dockerfile generation.
            # Branches on detection result (see the switch below).
            await _log(job_id, JobStage.ANALYZING, "Detecting deployment type")
            detection = await validate_detection(job_id, snapshot.root)
            if detection is None:
                return  # job already marked failed/needs_review by validate_detection

            # ---- ANALYZE (fingerprint) / Dockerfile branch ----
            if detection.needs_dockerfile:
                # Container path: generate a Dockerfile.
                await _log(job_id, JobStage.GENERATING, "Generating Dockerfile via Groq")

                # For v2 we don't call docker ourselves; expose generate only.
                result = await _generate_with_healing(job_id, snapshot.root, job.repo_url)

                if isinstance(result, DockerfileError):
                    error_msg = result.message + (f": {result.detail}" if result.detail else "")
                    await db.update_job_error(job_id, error_msg)
                    await _log(job_id, JobStage.FAILED, error_msg)
                    return

                await db.update_job_result(job_id, result.model_dump())
                # Write Dockerfile to local snapshot (for logging/UI display).
                dockerfile_path = os.path.join(snapshot.root, "Dockerfile")
                if not os.path.exists(dockerfile_path):
                    with open(dockerfile_path, "w", encoding="utf-8") as handle:
                        handle.write(result.dockerfile_content)
                await _log(
                    job_id,
                    JobStage.GENERATING,
                    "Dockerfile generated successfully (deploying via native buildpack — no GitHub commit needed)",
                )

                await _log(job_id, JobStage.DEPLOYING, "Deploying to Render")
                try:
                    # Try to get user's Render token first
                    render_token = None
                    render_oauth = get_render_oauth()

                    if render_oauth:
                        try:
                            render_token = await render_oauth.get_user_token(user_id)
                            await _log(job_id, JobStage.DEPLOYING, "Using user's Render account")
                        except Exception as e:
                            await _log(job_id, JobStage.DEPLOYING, f"User Render token not available: {str(e)}")

                    # Fallback to account-level token if user token not available
                    if not render_token and settings.render_api_token:
                        render_token = settings.render_api_token
                        await _log(job_id, JobStage.DEPLOYING, "Using account-level Render token")

                    if not render_token:
                        raise RenderDeployError("No Render token available - please connect your account or configure account-level token")

                    # Get user's environment variables
                    from .env_vars import get_env_var_manager
                    env_var_manager = get_env_var_manager()
                    env_vars = await env_var_manager.get_env_vars(job_id)

                    _owner, repo = parse_github_url(job.repo_url)
                    service_name = f"{repo}-{job.job_id[:8]}"
                    deployment_result = await deploy_to_render(
                        repo_path=snapshot.root,
                        service_name=service_name,
                        dockerfile_content=result.dockerfile_content,
                        access_token=render_token,
                        env_vars=env_vars,
                        repo_url=job.repo_url,
                        detected_framework=detection.detected_framework,
                        start_command=result.start_command,
                    )
                    await db.update_job_deployment(job_id, deployment_result.model_dump())
                    await _log(
                        job_id,
                        JobStage.DONE,
                        f"Live at {deployment_result.deployment_url}",
                    )
                except RenderDeployError as exc:
                    error_msg = f"Render deployment failed: {exc}"
                    await db.update_job_error(job_id, error_msg)
                    await _log(job_id, JobStage.FAILED, error_msg)
                    return

            # Try Railway deployment if Render is not available or user prefers Railway
            elif detection.deployment_type == DeploymentType.CONTAINER:
                await _log(job_id, JobStage.DEPLOYING, "Deploying to Railway")
                try:
                    # Try to get user's Railway token
                    railway_token = None
                    railway_oauth = get_railway_oauth()

                    if railway_oauth:
                        try:
                            railway_token = await railway_oauth.get_user_token(user_id)
                            await _log(job_id, JobStage.DEPLOYING, "Using user's Railway account")
                        except Exception as e:
                            await _log(job_id, JobStage.DEPLOYING, f"User Railway token not available: {str(e)}")

                    if not railway_token:
                        # Try to use Pulumi deployer for Railway
                        pulumi_deployer = get_pulumi_deployer()
                        await _log(job_id, JobStage.DEPLOYING, "Railway token not available, skipping Railway deployment")
                        return

                    # Get user's environment variables
                    from .env_vars import get_env_var_manager
                    env_var_manager = get_env_var_manager()
                    env_vars = await env_var_manager.get_env_vars(job_id)

                    _owner, repo = parse_github_url(job.repo_url)
                    service_name = f"{repo}-{job.job_id[:8]}"
                    
                    deployment_result = await pulumi_deployer.deploy(
                        platform="railway",
                        repo_path=snapshot.root,
                        service_name=service_name,
                        access_token=railway_token,
                        env_vars=env_vars,
                        repo_url=job.repo_url,
                    )
                    await db.update_job_deployment(job_id, deployment_result.model_dump())
                    await _log(
                        job_id,
                        JobStage.DONE,
                        f"Live at {deployment_result.deployment_url}",
                    )
                except PulumiDeployError as exc:
                    error_msg = f"Railway deployment failed: {exc}"
                    await db.update_job_error(job_id, error_msg)
                    await _log(job_id, JobStage.FAILED, error_msg)
                    return

            elif detection.deployment_type in (DeploymentType.STATIC, DeploymentType.VERCEL_NATIVE):
                # Static / Vercel-native path: no Dockerfile, deploy straight to Vercel.
                await _log(job_id, JobStage.DEPLOYING, f"Deploying {detection.detected_framework} to Vercel")
                try:
                    # Try to get user's Vercel token first
                    vercel_token = None
                    vercel_oauth = get_vercel_oauth()
                    
                    await _log(job_id, JobStage.DEPLOYING, f"User ID: {user_id}")
                    await _log(job_id, JobStage.DEPLOYING, f"Vercel OAuth available: {vercel_oauth is not None}")
                    await _log(job_id, JobStage.DEPLOYING, f"Account Vercel token configured: {bool(settings.vercel_api_token)}")
                    
                    if vercel_oauth:
                        try:
                            vercel_token = await vercel_oauth.get_user_token(user_id)
                            await _log(job_id, JobStage.DEPLOYING, "Using user's Vercel account")
                        except Exception as e:
                            await _log(job_id, JobStage.DEPLOYING, f"User Vercel token not available: {str(e)}")
                    
                    # Fallback to account-level token if user token not available
                    if not vercel_token and settings.vercel_api_token:
                        vercel_token = settings.vercel_api_token
                        await _log(job_id, JobStage.DEPLOYING, f"Using account-level Vercel token: {vercel_token[:10]}...")
                    
                    if not vercel_token:
                        await _log(job_id, JobStage.DEPLOYING, f"No Vercel token available. Account token configured: {bool(settings.vercel_api_token)}")
                        raise VercelDeployError("No Vercel token available - please connect your account or configure account-level token")

                    # Get user's environment variables
                    from .env_vars import get_env_var_manager
                    env_var_manager = get_env_var_manager()
                    env_vars = await env_var_manager.get_env_vars(job_id)

                    try:
                        owner, repo = parse_github_url(job.repo_url)
                        project_name = f"{owner}-{repo}"
                    except Exception:
                        project_name = f"app-{re.sub(r'[^a-zA-Z0-9-_]', '-', job.job_id)[:52]}"

                    await _log(job_id, JobStage.DEPLOYING, f"Starting Vercel deployment for {project_name}")
                    deployment_result = await deploy_to_vercel(
                        repo_path=snapshot.root,
                        project_name=project_name,
                        framework=detection.detected_framework,
                        access_token=vercel_token,
                        env_vars=env_vars,
                    )
                    await _log(job_id, JobStage.DEPLOYING, f"Vercel deployment completed: {deployment_result.deployment_url}")
                    await db.update_job_deployment(job_id, deployment_result.model_dump())
                    await _log(
                        job_id,
                        JobStage.DONE,
                        f"Live at {deployment_result.deployment_url}",
                    )
                except VercelDeployError as exc:
                    error_msg = f"Vercel deployment failed: {exc}"
                    await db.update_job_error(job_id, error_msg)
                    await _log(job_id, JobStage.FAILED, error_msg)
                    return
            else:
                # AMBIGUOUS handled inside validate_detection (job paused for review).
                pass

    except (CloneError, InvalidRepoURL, GHInvalidURL) as exc:
        error_msg = f"Clone/validation failed: {exc}"
        await db.update_job_error(job_id, error_msg)
        await _log(job_id, JobStage.FAILED, error_msg)
    except Exception as exc:  # broad safety net — never let a task die silently
        error_msg = f"Unexpected error: {exc}"
        await db.update_job_error(job_id, error_msg)
        await _log(job_id, JobStage.FAILED, error_msg)


async def validate_detection(job_id: str, repo_path: str) -> Optional[DetectionResult]:
    """Run deployment detection for a job and persist + broadcast the result.

    * Persists a JobDetection onto the job and emits a
      {"step": "detection", "status": "complete", "result": {...}} WebSocket
      event so the dashboard shows it live.
    * For AMBIGUOUS results, flips the job into NEEDS_REVIEW and pauses the
      pipeline until the user calls POST /jobs/{id}/override-detection.
    * Returns the DetectionResult, or None if the pipeline should stop (paused
      for review or detection failed hard).

    The whole detection call is wrapped so a bad repo can never kill the job.

    Args:
        job_id: The job identifier
        repo_path: Path to the cloned repository

    Returns:
        DetectionResult or None if pipeline should stop
    """
    db = get_db_service()

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
    job_detection = JobDetection(
        deployment_type=result.deployment_type,
        needs_dockerfile=result.needs_dockerfile,
        detected_framework=result.detected_framework,
        entry_point=result.entry_point,
        listen_port=result.listen_port,
        reasoning=result.reasoning,
        detection_method=result.detection_method,
    )

    await db.update_job_detection(job_id, job_detection)

    # Update status if ambiguous
    if result.deployment_type == DeploymentType.AMBIGUOUS:
        await db.update_job_status(job_id, JobStage.NEEDS_REVIEW.value)

    # Emit the structured detection event (stable dashboard payload).
    await hub.publish(_detection_event(job_id, result))

    # If ambiguous, pause the pipeline for manual review.
    if result.deployment_type == DeploymentType.AMBIGUOUS:
        reason = result.ambiguous_reason or result.reasoning or "unknown"
        await _log(
            job_id,
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

    Args:
        job_id: The job identifier
        deployment_type: The deployment type to set

    Returns:
        DetectionResult reflecting the manual classification
    """
    db = get_db_service()
    job_record = await db.get_job(job_id)
    if job_record is None:
        raise JobNotFound(job_id)

    detection = job_record.get("detection")
    framework = detection.get("detected_framework") if detection else "unknown"
    entry_point = detection.get("entry_point") if detection else None
    listen_port = detection.get("listen_port") if detection else None
    method = detection.get("detection_method") if detection else "rule_based"

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

    job_detection = JobDetection(
        deployment_type=deployment_type,
        needs_dockerfile=result.needs_dockerfile,
        detected_framework=framework,
        entry_point=entry_point,
        listen_port=listen_port,
        reasoning=result.reasoning,
        detection_method=method,
    )

    await db.update_job_detection(job_id, job_detection)
    await db.update_job_status(job_id, JobStage.QUEUED.value)  # unblock the paused pipeline
    return result


async def _generate_with_healing(
    job_id: str, repo_root: str, repo_url: str
) -> DockerfileResult | DockerfileError:
    """Run generate_dockerfile, wiring an optional build callback.

    The build callback is where the DevOps engineer's docker build would plug in.
    In v2 we leave it as a no-op (generate only) — the API surface accepts a real
    build_fn enabling the bounded self-heal loop that actually builds containers.

    Args:
        job_id: The job identifier
        repo_root: Path to the repository
        repo_url: The repository URL

    Returns:
        DockerfileResult or DockerfileError
    """
    def build_fn(dockerfile_content: str) -> Optional[str]:
        # TODO(DevOps): replace this no-op with a real docker build
        # (docker-py or a subprocess) that returns an error string on failure.
        # Reporting None = "build succeeded", which in our v2 flow stops the
        # pipeline after generation and marks the job done with a result.
        return None

    return await generate_dockerfile(
        repo_path=repo_root,
        build_fn=build_fn,
        repo_url=repo_url,
        job_id=job_id,
    )


async def _commit_dockerfile_to_github(
    repo_path: str, repo_url: str, github_token: str, job_id: str
) -> None:
    """Commit the generated Dockerfile to the GitHub repository.

    This is necessary for Render deployments, which deploy from the GitHub repo.

    Args:
        repo_path: Local path to the cloned repository
        repo_url: GitHub repository URL
        github_token: GitHub access token for authentication
        job_id: Job ID for logging
    """
    import httpx

    # Read the Dockerfile
    dockerfile_path = os.path.join(repo_path, "Dockerfile")
    if not os.path.exists(dockerfile_path):
        raise FileNotFoundError("Dockerfile not found in repository")

    with open(dockerfile_path, "r", encoding="utf-8") as f:
        dockerfile_content = f.read()

    # Encode content for GitHub API
    content_b64 = base64.b64encode(dockerfile_content.encode("utf-8")).decode("utf-8")

    # Parse repo URL to get owner and repo name
    try:
        owner, repo = parse_github_url(repo_url)
    except Exception as e:
        raise ValueError(f"Failed to parse GitHub URL: {e}")

    # Check if Dockerfile already exists in the repo
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/Dockerfile"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    async with httpx.AsyncClient() as client:
        # Check if file exists
        response = await client.get(api_url, headers=headers)
        sha = None
        if response.status_code == 200:
            # File exists, get its SHA for update
            sha = response.json().get("sha")
        elif response.status_code != 404:
            raise Exception(f"Failed to check Dockerfile existence: {response.status_code} {response.text}")

        # Create or update the file
        data = {
            "message": f"Add Dockerfile for deployment [job: {job_id[:8]}]",
            "content": content_b64,
        }
        if sha:
            data["sha"] = sha

        response = await client.put(api_url, headers=headers, json=data)
        if response.status_code not in (200, 201):
            raise Exception(f"Failed to commit Dockerfile: {response.status_code} {response.text}")


async def get_job(job_id: str) -> JobStatus:
    """Return the current JobStatus snapshot; raises JobNotFound if unknown.

    Args:
        job_id: The job identifier

    Returns:
        JobStatus with current state from database
    """
    db = get_db_service()
    job_record = await db.get_job(job_id)
    if job_record is None:
        raise JobNotFound(job_id)

    # Reconstruct JobStatus from database record
    logs_data = job_record.get("logs", "[]")
    logs = []
    if isinstance(logs_data, str):
        logs_data = json.loads(logs_data)
    for log_entry in logs_data:
        logs.append(
            JobEvent(
                job_id=log_entry["job_id"],
                stage=JobStage(log_entry["stage"]),
                message=log_entry["message"],
                timestamp=log_entry.get("timestamp"),
            )
        )

    return JobStatus(
        job_id=job_record["job_id"],
        status=JobStage(job_record["status"]),
        repo_url=job_record["repo_url"],
        created_at=job_record["created_at"],
        logs=logs,
        result=job_record.get("result"),
        error=job_record.get("error"),
        detection=job_record.get("detection"),
        deployment=job_record.get("deployment"),
        repo_path=job_record.get("repo_path"),
    )


async def list_jobs(user_id: str) -> list[JobStatus]:
    """Return all jobs for a user, most recently created first.

    Args:
        user_id: The user ID to filter jobs by

    Returns:
        List of JobStatus objects
    """
    db = get_db_service()
    job_records = await db.list_user_jobs(user_id)

    jobs = []
    for job_record in job_records:
        logs_data = job_record.get("logs", "[]")
        logs = []
        if isinstance(logs_data, str):
            logs_data = json.loads(logs_data)
        for log_entry in logs_data:
            logs.append(
                JobEvent(
                    job_id=log_entry["job_id"],
                    stage=JobStage(log_entry["stage"]),
                    message=log_entry["message"],
                    timestamp=log_entry.get("timestamp"),
                )
            )

        jobs.append(
            JobStatus(
                job_id=job_record["job_id"],
                status=JobStage(job_record["status"]),
                repo_url=job_record["repo_url"],
                created_at=job_record["created_at"],
                logs=logs,
                result=job_record.get("result"),
                error=job_record.get("error"),
                detection=job_record.get("detection"),
                deployment=job_record.get("deployment"),
                repo_path=job_record.get("repo_path"),
            )
        )

    jobs.sort(key=lambda j: j.created_at, reverse=True)
    return jobs
