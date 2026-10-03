"""Database service for Supabase operations."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from supabase import Client, create_client

from ..config import settings, supabase_configured
from ..contracts import JobDetection, JobEvent, JobStage


class DatabaseService:
    """Service for all Supabase database operations."""

    def __init__(self):
        """Initialize Supabase client."""
        if not supabase_configured():
            raise RuntimeError("Supabase not configured")
        self.client: Client = create_client(
            settings.supabase_url, settings.supabase_service_role_key
        )

    # User credentials operations
    async def store_user_credential(
        self,
        user_id: str,
        platform: str,
        access_token: str,
        refresh_token: Optional[str] = None,
        token_expires_at: Optional[datetime] = None,
    ) -> None:
        """Store or update user's OAuth credentials for a platform."""
        data = {
            "user_id": user_id,
            "platform": platform,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_expires_at": token_expires_at.isoformat() if token_expires_at else None,
        }

        # Upsert using on_conflict
        try:
            self.client.table("user_credentials").upsert(data).execute()
        except Exception as e:
            print(f"Database upsert error for credentials: {e}")
            raise

    async def get_user_credential(self, user_id: str, platform: str) -> Optional[dict]:
        """Get user's OAuth credentials for a platform."""
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
        """Delete user's OAuth credentials for a platform."""
        self.client.table("user_credentials").delete().eq("user_id", user_id).eq(
            "platform", platform
        ).execute()

    # Job operations
    async def create_job(
        self,
        user_id: str,
        job_id: str,
        repo_url: str,
        status: str = "queued",
    ) -> dict:
        """Create a new job record in the database."""
        data = {
            "user_id": user_id,
            "job_id": job_id,
            "status": status,
            "repo_url": repo_url,
            "logs": [],
        }

        try:
            result = self.client.table("jobs").insert(data).select().execute()
            if result.data and len(result.data) > 0:
                return result.data[0]
            else:
                # Fallback: return the original data if insert succeeded but no return data
                return data
        except Exception as e:
            # If insert fails (e.g. foreign key constraint for unauthenticated user),
            # log warning and return data dictionary so pipeline continues in memory
            print(f"Database insert error: {e}")
            return data


    async def get_job(self, job_id: str) -> Optional[dict]:
        """Get a job by job_id."""
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
        """Update a job record."""
        try:
            self.client.table("jobs").update(updates).eq("job_id", job_id).execute()
        except Exception as e:
            print(f"Database update_job error: {e}")
            raise

    async def list_user_jobs(self, user_id: str) -> list[dict]:
        """List all jobs for a user, most recent first."""
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
        """Add a log event to a job."""
        job = await self.get_job(job_id)
        if not job:
            return

        # Handle both JSON string and direct list cases
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
        """Update job detection information."""
        await self.update_job(
            job_id,
            {
                "detection": detection.model_dump(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def update_job_deployment(self, job_id: str, deployment: dict) -> None:
        """Update job deployment information."""
        await self.update_job(
            job_id,
            {
                "deployment": deployment,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def update_job_result(self, job_id: str, result: dict) -> None:
        """Update job result (Dockerfile generation result)."""
        await self.update_job(
            job_id,
            {
                "result": result,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def update_job_error(self, job_id: str, error: str) -> None:
        """Update job error information."""
        await self.update_job(
            job_id,
            {
                "error": error,
                "status": "failed",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def update_job_status(self, job_id: str, status: str) -> None:
        """Update job status."""
        await self.update_job(
            job_id,
            {
                "status": status,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def update_job_repo_path(self, job_id: str, repo_path: str) -> None:
        """Update job repository path."""
        await self.update_job(
            job_id,
            {
                "repo_path": repo_path,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    # Environment variable operations
    async def store_env_vars(self, job_id: str, env_vars: dict[str, str]) -> None:
        """Store environment variables for a job."""
        # First, get the database job ID from the job_id string
        job = await self.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        db_job_id = job["id"]

        # Delete existing env vars for this job
        self.client.table("environment_variables").delete().eq("job_id", db_job_id).execute()

        # Insert new env vars
        for key, value in env_vars.items():
            self.client.table("environment_variables").insert(
                {"job_id": db_job_id, "key": key, "value": value}
            ).execute()

    async def get_env_vars(self, job_id: str) -> dict[str, str]:
        """Get environment variables for a job."""
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


# Global database service instance
_db_service: Optional[DatabaseService] = None


def get_db_service() -> DatabaseService:
    """Get or create the global database service instance."""
    global _db_service
    if _db_service is None:
        _db_service = DatabaseService()
    return _db_service