"""Shared JSON contracts (Pydantic models).

This module is the contract surface the whole pipeline depends on:
  * RepoFingerprint  -> what we feed to the LLM (never the whole repo)
  * DockerfileResult -> what we return to the DevOps engineer
  * DockerfileError  -> the error variant of the above
  * JobEvent/JobStatus -> the WebSocket event shape we emit

Teammates (DevOps + Frontend) should treat these field names as stable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Stage / status enums
# ---------------------------------------------------------------------------
class JobStage(str, Enum):
    """Lifecycle stage of a job. Each stage maps to a phase of the pipeline.

    `queued`  -> accepted, waiting for a worker slot
    `cloning` -> downloading the repo
    `analyzing` -> building the fingerprint
    `generating` -> first Groq pass to produce a Dockerfile
    `building` -> (owned by DevOps) Docker build in progress
    `healing` -> a build failed and we are patching via Groq
    `done` -> success, result available
    `failed` -> terminal failure (error surfaced)
    """

    QUEUED = "queued"
    CLONING = "cloning"
    ANALYZING = "analyzing"
    GENERATING = "generating"
    BUILDING = "building"
    HEALING = "healing"
    DONE = "done"
    FAILED = "failed"


class Framework(str, Enum):
    """Only the 3 v1 stacks are supported. Anything else is rejected at analysis."""

    NODE = "node"
    PYTHON = "python"
    STATIC = "static"


# ---------------------------------------------------------------------------
# Repo fingerprint  ->  the artifact we send to the model
# ---------------------------------------------------------------------------
class EntryPoint(BaseModel):
    """Guessed application entry point (path relative to repo root) + a preview."""

    path: Optional[str] = None
    content: Optional[str] = None


class RepoFingerprint(BaseModel):
    """Compact, curated summary of a repo. This — and only this — goes to Groq.

    Rationale for each field:
      * repo_url          -> context, plus idempotency/debugging
      * file_tree         -> filtered list of relative paths, used to guess layout
      * manifests         -> raw contents of manifest files (package.json etc.)
      * entry_point       -> best-guess entry file + a snippet
      * existing_dockerfile/compose -> reuse/learn from any already-present config
    """

    repo_url: str
    file_tree: list[str] = Field(default_factory=list)
    manifests: dict[str, str] = Field(default_factory=dict)
    entry_point: EntryPoint = Field(default_factory=EntryPoint)
    existing_dockerfile: Optional[str] = None
    existing_compose: Optional[str] = None


# ---------------------------------------------------------------------------
# Dockerfile response  ->  what the LLM and this service return
# ---------------------------------------------------------------------------
class DockerfileResult(BaseModel):
    """Structured Dockerfile generation output.

    `framework` must be one of the 3 supported enums; `dockerfile_content` is the
    finished Dockerfile string. `metadata` carries human-readable reasoning for
    debugging (not used for execution).
    """

    language: str
    framework: Framework
    entry_point: Optional[str] = None
    port: Optional[int] = None
    start_command: Optional[str] = None
    dockerfile_content: str
    metadata: dict = Field(default_factory=dict)


class DockerfileError(BaseModel):
    """Error variant returned by generate_dockerfile instead of a result."""

    message: str
    stage: str
    detail: Optional[str] = None


# ---------------------------------------------------------------------------
# Job + event shapes (WebSocket contract)
# ---------------------------------------------------------------------------
class JobEvent(BaseModel):
    """One status/log event emitted throughout a job's life.

    Shape: {job_id, stage, message, timestamp}  <- stable WebSocket payload.
    """

    job_id: str
    stage: JobStage
    message: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class JobStatus(BaseModel):
    """Snapshot returned by GET /jobs/{id}."""

    job_id: str
    status: JobStage
    repo_url: str
    logs: list[JobEvent] = Field(default_factory=list)
    result: Optional[DockerfileResult] = None
    error: Optional[str] = None
    # Populated by the DevOps deploy step when a live host URL is available.
    deploy_url: Optional[str] = None


class DeploymentRecord(BaseModel):
    """One deploy attempt — backs the history / rollback view."""

    id: str
    job_id: Optional[str] = None
    provider: str = "render"
    service_id: Optional[str] = None
    live_url: Optional[str] = None
    image_tag: Optional[str] = None
    status: str
    is_active: bool = False
    created_at: Optional[datetime] = None
