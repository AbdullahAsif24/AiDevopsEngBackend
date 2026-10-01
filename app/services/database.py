"""Database service — Supabase when configured, else in-memory fallback."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from ..config import settings, supabase_configured
from ..contracts import JobDetection, JobEvent, JobStage


class _MemoryStore:
    """Process-local store used when Supabase is not configured."""

    def __init__(self) -> None:
        self.credentials: dict[tuple[str, str], dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.env_vars: dict[str, dict[str, str]] = {}


_memory = _MemoryStore()


class DatabaseService:
    """Service for credential / job persistence."""

    def __init__(self):
        self._use_memory = not supabase_configured()
        self.client = None
        if not self._use_memory:
            from supabase import create_client

            self.client = create_client(
                settings.supabase_url, settings.supabase_service_role_key
            )

    async def store_user_credential(
        self,
        user_id: str,
        platform: str,
        access_token: str,
        refresh_token: Optional[str] = None,
        token_expires_at: Optional[datetime] = None,
    ) -> None:
        data = {
            "user_id": user_id,
            "platform": platform,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_expires_at": token_expires_at.isoformat() if token_expires_at else None,
        }
        if self._use_memory:
            _memory.credentials[(user_id, platform)] = data
            return
        try:
            self.client.table("user_credentials").upsert(data).execute()
        except Exception as e:
            print(f"Database upsert error for credentials: {e}")
            raise

    async def get_user_credential(self, user_id: str, platform: str) -> Optional[dict]:
        if self._use_memory:
            return _memory.credentials.get((user_id, platform))
        try:
            result = (
                self.client.table("user_credentials")
                .select("*")
                .eq("user_id", user_id)
                .eq("platform", platform)
                .limit(1)
                .execute()
            )
            if result.data and len(result.data) > 0:
                return result.data[0]
        except Exception as e:
            print(f"Database get_user_credential error: {e}")
        return None

    async def delete_user_credential(self, user_id: str, platform: str) -> None:
        if self._use_memory:
            _memory.credentials.pop((user_id, platform), None)
            return
        self.client.table("user_credentials").delete().eq("user_id", user_id).eq(
            "platform", platform
        ).execute()

    async def create_job(
        self,
        user_id: str,
        job_id: str,
        repo_url: str,
        status: str = "queued",
    ) -> dict:
        data = {
            "user_id": user_id,
            "job_id": job_id,
            "status": status,
            "repo_url": repo_url,
            "logs": [],
            "id": job_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if self._use_memory:
            _memory.jobs[job_id] = data
            return data
        try:
            result = self.client.table("jobs").insert(data).select().execute()
            if result.data and len(result.data) > 0:
                return result.data[0]
            return data
        except Exception as e:
            print(f"Database insert error: {e}")
            raise

    async def get_job(self, job_id: str) -> Optional[dict]:
        if self._use_memory:
            return _memory.jobs.get(job_id)
        try:
            result = (
                self.client.table("jobs").select("*").eq("job_id", job_id).limit(1).execute()
            )
            if result.data and len(result.data) > 0:
                return result.data[0]
        except Exception as e:
            print(f"Database get_job error: {e}")
        return None

    async def update_job(self, job_id: str, updates: dict[str, Any]) -> None:
        if self._use_memory:
            job = _memory.jobs.get(job_id)
            if job:
                job.update(updates)
            return
        try:
            self.client.table("jobs").update(updates).eq("job_id", job_id).execute()
        except Exception as e:
            print(f"Database update_job error: {e}")
            raise

    async def list_user_jobs(self, user_id: str) -> list[dict]:
        if self._use_memory:
            jobs = [j for j in _memory.jobs.values() if j.get("user_id") == user_id]
            jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
            return jobs
        try:
            result = (
                self.client.table("jobs")
                .select("*")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .execute()
            )
            return result.data if result.data else []
        except Exception as e:
            print(f"Database list_user_jobs error: {e}")
            return []

    async def add_job_log(self, job_id: str, event: JobEvent) -> None:
        job = await self.get_job(job_id)
        if not job:
            return
        logs_data = job.get("logs", [])
        if isinstance(logs_data, str):
            logs = json.loads(logs_data)
        else:
            logs = logs_data if logs_data else []
        logs.append(
            {
                "job_id": event.job_id,
                "stage": event.stage.value,
                "message": event.message,
                "timestamp": event.timestamp.isoformat(),
            }
        )
        await self.update_job(
            job_id,
            {
                "logs": logs,
                "status": event.stage.value,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def update_job_detection(self, job_id: str, detection: JobDetection) -> None:
        await self.update_job(
            job_id,
            {
                "detection": detection.model_dump(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def update_job_deployment(self, job_id: str, deployment: dict) -> None:
        await self.update_job(
            job_id,
            {
                "deployment": deployment,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def update_job_result(self, job_id: str, result: dict) -> None:
        await self.update_job(
            job_id,
            {
                "result": result,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def update_job_error(self, job_id: str, error: str) -> None:
        await self.update_job(
            job_id,
            {
                "error": error,
                "status": "failed",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def update_job_status(self, job_id: str, status: str) -> None:
        await self.update_job(
            job_id,
            {
                "status": status,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def update_job_repo_path(self, job_id: str, repo_path: str) -> None:
        await self.update_job(
            job_id,
            {
                "repo_path": repo_path,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def store_env_vars(self, job_id: str, env_vars: dict[str, str]) -> None:
        if self._use_memory:
            _memory.env_vars[job_id] = dict(env_vars)
            return
        job = await self.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        db_job_id = job["id"]
        self.client.table("environment_variables").delete().eq("job_id", db_job_id).execute()
        for key, value in env_vars.items():
            self.client.table("environment_variables").insert(
                {"job_id": db_job_id, "key": key, "value": value}
            ).execute()

    async def get_env_vars(self, job_id: str) -> dict[str, str]:
        if self._use_memory:
            return dict(_memory.env_vars.get(job_id) or {})
        job = await self.get_job(job_id)
        if not job:
            return {}
        db_job_id = job["id"]
        result = (
            self.client.table("environment_variables")
            .select("key, value")
            .eq("job_id", db_job_id)
            .execute()
        )
        return {item["key"]: item["value"] for item in result.data}


_db_service: Optional[DatabaseService] = None


def get_db_service() -> DatabaseService:
    global _db_service
    if _db_service is None:
        _db_service = DatabaseService()
    return _db_service
