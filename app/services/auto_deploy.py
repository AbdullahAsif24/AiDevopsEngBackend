"""Auto-deploy on push service.

Handles:
  * GitHub Webhook secret verification (HMAC SHA-256)
  * Push event payload parsing and branch matching
  * Triggering automated deployment pipeline via `schedule_job`
  * Storing and querying webhook push event deliveries
  * Simulation / test-push triggering for instant testing
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

from ..contracts import (
    AutoDeployConfig,
    CreateAutoDeployRequest,
    UpdateAutoDeployRequest,
    WebhookEventRecord,
)
from ..config import supabase_configured
from ..services.database import get_db_service
from ..services.jobs import make_job, schedule_job


class AutoDeployManager:
    """Manages auto-deploy configurations and push webhook events."""

    def __init__(self):
        # In-memory stores for resilience / standalone mode
        self._configs: dict[str, AutoDeployConfig] = {}
        self._events: list[WebhookEventRecord] = []

    def _get_base_webhook_url(self, config_id: str) -> str:
        # Standard default webhook endpoint
        return f"http://localhost:8000/webhooks/github/{config_id}"

    async def list_configs(self, user_id: str) -> list[AutoDeployConfig]:
        """List all auto-deploy configurations for a user."""
        if supabase_configured():
            try:
                db = get_db_service()
                res = (
                    db.client.table("auto_deploy_configs")
                    .select("*")
                    .eq("user_id", user_id)
                    .order("created_at", desc=True)
                    .execute()
                )
                if res.data:
                    return [
                        AutoDeployConfig(
                            id=item["id"],
                            user_id=item["user_id"],
                            repo_url=item["repo_url"],
                            branch=item.get("branch", "main"),
                            is_active=item.get("is_active", True),
                            webhook_secret=item["webhook_secret"],
                            webhook_url=item.get("webhook_url", self._get_base_webhook_url(item["id"])),
                            auto_rollback=item.get("auto_rollback", True),
                            created_at=datetime.fromisoformat(item["created_at"]),
                            updated_at=datetime.fromisoformat(item.get("updated_at", item["created_at"])),
                        )
                        for item in res.data
                    ]
            except Exception as e:
                print(f"[AutoDeploy] Supabase list_configs fallback: {e}")

        # In-memory fallback
        return [c for c in self._configs.values() if c.user_id == user_id]

    async def get_config(self, config_id: str) -> Optional[AutoDeployConfig]:
        """Get an auto-deploy configuration by id."""
        if supabase_configured():
            try:
                db = get_db_service()
                res = (
                    db.client.table("auto_deploy_configs")
                    .select("*")
                    .eq("id", config_id)
                    .limit(1)
                    .execute()
                )
                if res.data and len(res.data) > 0:
                    item = res.data[0]
                    return AutoDeployConfig(
                        id=item["id"],
                        user_id=item["user_id"],
                        repo_url=item["repo_url"],
                        branch=item.get("branch", "main"),
                        is_active=item.get("is_active", True),
                        webhook_secret=item["webhook_secret"],
                        webhook_url=item.get("webhook_url", self._get_base_webhook_url(item["id"])),
                        auto_rollback=item.get("auto_rollback", True),
                        created_at=datetime.fromisoformat(item["created_at"]),
                        updated_at=datetime.fromisoformat(item.get("updated_at", item["created_at"])),
                    )
            except Exception as e:
                print(f"[AutoDeploy] Supabase get_config fallback: {e}")

        return self._configs.get(config_id)

    async def create_config(self, user_id: str, req: CreateAutoDeployRequest) -> AutoDeployConfig:
        """Create a new auto-deploy configuration with generated webhook secret."""
        config_id = str(uuid.uuid4())
        webhook_secret = secrets.token_hex(20)
        webhook_url = self._get_base_webhook_url(config_id)
        now = datetime.now(timezone.utc)

        config = AutoDeployConfig(
            id=config_id,
            user_id=user_id,
            repo_url=req.repo_url.strip(),
            branch=req.branch.strip() or "main",
            is_active=True,
            webhook_secret=webhook_secret,
            webhook_url=webhook_url,
            auto_rollback=req.auto_rollback,
            created_at=now,
            updated_at=now,
        )

        if supabase_configured():
            try:
                db = get_db_service()
                db.client.table("auto_deploy_configs").insert({
                    "id": config.id,
                    "user_id": config.user_id,
                    "repo_url": config.repo_url,
                    "branch": config.branch,
                    "is_active": config.is_active,
                    "webhook_secret": config.webhook_secret,
                    "webhook_url": config.webhook_url,
                    "auto_rollback": config.auto_rollback,
                    "created_at": config.created_at.isoformat(),
                    "updated_at": config.updated_at.isoformat(),
                }).execute()
            except Exception as e:
                print(f"[AutoDeploy] Supabase create_config fallback: {e}")

        self._configs[config_id] = config
        return config

    async def update_config(
        self, config_id: str, user_id: str, req: UpdateAutoDeployRequest
    ) -> Optional[AutoDeployConfig]:
        """Update auto-deploy configuration."""
        config = await self.get_config(config_id)
        if not config or config.user_id != user_id:
            return None

        now = datetime.now(timezone.utc)
        new_branch = req.branch.strip() if req.branch is not None else config.branch
        new_active = req.is_active if req.is_active is not None else config.is_active
        new_rollback = req.auto_rollback if req.auto_rollback is not None else config.auto_rollback

        updated = AutoDeployConfig(
            id=config.id,
            user_id=config.user_id,
            repo_url=config.repo_url,
            branch=new_branch,
            is_active=new_active,
            webhook_secret=config.webhook_secret,
            webhook_url=config.webhook_url,
            auto_rollback=new_rollback,
            created_at=config.created_at,
            updated_at=now,
        )

        if supabase_configured():
            try:
                db = get_db_service()
                db.client.table("auto_deploy_configs").update({
                    "branch": updated.branch,
                    "is_active": updated.is_active,
                    "auto_rollback": updated.auto_rollback,
                    "updated_at": updated.updated_at.isoformat(),
                }).eq("id", config_id).execute()
            except Exception as e:
                print(f"[AutoDeploy] Supabase update_config fallback: {e}")

        self._configs[config_id] = updated
        return updated

    async def delete_config(self, config_id: str, user_id: str) -> bool:
        """Delete an auto-deploy configuration."""
        config = await self.get_config(config_id)
        if not config or config.user_id != user_id:
            return False

        if supabase_configured():
            try:
                db = get_db_service()
                db.client.table("auto_deploy_configs").delete().eq("id", config_id).execute()
            except Exception as e:
                print(f"[AutoDeploy] Supabase delete_config fallback: {e}")

        self._configs.pop(config_id, None)
        return True

    def verify_signature(self, raw_body: bytes, signature_header: Optional[str], secret: str) -> bool:
        """Verify GitHub's X-Hub-Signature-256 header."""
        if not signature_header:
            return False
        if not signature_header.startswith("sha256="):
            return False

        expected_sig = signature_header[7:]
        computed_sig = hmac.new(
            secret.encode("utf-8"),
            raw_body,
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(expected_sig, computed_sig)

    async def record_event(self, event: WebhookEventRecord) -> None:
        """Record a webhook delivery event."""
        self._events.insert(0, event)
        if len(self._events) > 100:
            self._events.pop()

        if supabase_configured():
            try:
                db = get_db_service()
                db.client.table("webhook_events").insert({
                    "id": event.id,
                    "config_id": event.config_id,
                    "user_id": event.user_id,
                    "repo_url": event.repo_url,
                    "branch": event.branch,
                    "commit_sha": event.commit_sha,
                    "commit_message": event.commit_message,
                    "committer": event.committer,
                    "status": event.status,
                    "job_id": event.job_id,
                    "error_message": event.error_message,
                    "created_at": event.created_at.isoformat(),
                }).execute()
            except Exception as e:
                print(f"[AutoDeploy] Supabase record_event fallback: {e}")

    async def list_events(self, user_id: str) -> list[WebhookEventRecord]:
        """List webhook push delivery history for a user."""
        if supabase_configured():
            try:
                db = get_db_service()
                res = (
                    db.client.table("webhook_events")
                    .select("*")
                    .eq("user_id", user_id)
                    .order("created_at", desc=True)
                    .limit(50)
                    .execute()
                )
                if res.data:
                    return [
                        WebhookEventRecord(
                            id=item["id"],
                            config_id=item.get("config_id"),
                            user_id=item["user_id"],
                            repo_url=item["repo_url"],
                            branch=item["branch"],
                            commit_sha=item["commit_sha"],
                            commit_message=item["commit_message"],
                            committer=item["committer"],
                            status=item["status"],
                            job_id=item.get("job_id"),
                            error_message=item.get("error_message"),
                            created_at=datetime.fromisoformat(item["created_at"]),
                        )
                        for item in res.data
                    ]
            except Exception as e:
                print(f"[AutoDeploy] Supabase list_events fallback: {e}")

        return [e for e in self._events if e.user_id == user_id]

    async def handle_push_event(
        self,
        config: AutoDeployConfig,
        repo_url: str,
        branch: str,
        commit_sha: str,
        commit_message: str,
        committer: str,
    ) -> WebhookEventRecord:
        """Evaluate push event and trigger deployment job if branch matches."""
        now = datetime.now(timezone.utc)
        event_id = str(uuid.uuid4())

        if not config.is_active:
            record = WebhookEventRecord(
                id=event_id,
                config_id=config.id,
                user_id=config.user_id,
                repo_url=repo_url,
                branch=branch,
                commit_sha=commit_sha[:7],
                commit_message=commit_message,
                committer=committer,
                status="skipped",
                error_message="Auto-deploy is disabled for this repository",
                created_at=now,
            )
            await self.record_event(record)
            return record

        # Check branch match
        target_branch = config.branch.strip()
        if branch != target_branch and target_branch != "*":
            record = WebhookEventRecord(
                id=event_id,
                config_id=config.id,
                user_id=config.user_id,
                repo_url=repo_url,
                branch=branch,
                commit_sha=commit_sha[:7],
                commit_message=commit_message,
                committer=committer,
                status="skipped",
                error_message=f"Pushed branch '{branch}' does not match watched branch '{target_branch}'",
                created_at=now,
            )
            await self.record_event(record)
            return record

        # Trigger auto-deployment!
        try:
            job = make_job(config.user_id, repo_url)
            await schedule_job(job, config.user_id)

            record = WebhookEventRecord(
                id=event_id,
                config_id=config.id,
                user_id=config.user_id,
                repo_url=repo_url,
                branch=branch,
                commit_sha=commit_sha[:7],
                commit_message=commit_message,
                committer=committer,
                status="triggered",
                job_id=job.job_id,
                created_at=now,
            )
            await self.record_event(record)
            return record
        except Exception as e:
            record = WebhookEventRecord(
                id=event_id,
                config_id=config.id,
                user_id=config.user_id,
                repo_url=repo_url,
                branch=branch,
                commit_sha=commit_sha[:7],
                commit_message=commit_message,
                committer=committer,
                status="failed",
                error_message=str(e),
                created_at=now,
            )
            await self.record_event(record)
            return record

    async def trigger_test_push(
        self,
        user_id: str,
        config_id: Optional[str] = None,
        repo_url: Optional[str] = None,
        branch: Optional[str] = "main",
        commit_message: Optional[str] = "Simulated commit: update production build",
        committer: Optional[str] = "devops-engineer",
    ) -> WebhookEventRecord:
        """Trigger an instant simulated push deployment for demonstration/testing."""
        if config_id:
            config = await self.get_config(config_id)
        else:
            configs = await self.list_configs(user_id)
            config = configs[0] if configs else None

        target_repo = repo_url or (config.repo_url if config else "https://github.com/fastapi/fastapi")
        target_branch = branch or (config.branch if config else "main")
        commit_sha = secrets.token_hex(4)

        if not config:
            # Create a transient config for this test
            config = await self.create_config(
                user_id,
                CreateAutoDeployRequest(repo_url=target_repo, branch=target_branch, auto_rollback=True),
            )

        return await self.handle_push_event(
            config=config,
            repo_url=target_repo,
            branch=target_branch,
            commit_sha=commit_sha,
            commit_message=commit_message or "Fix: auto-deploy commit triggered",
            committer=committer or "developer",
        )


_auto_deploy_manager: Optional[AutoDeployManager] = None


def get_auto_deploy_manager() -> AutoDeployManager:
    global _auto_deploy_manager
    if _auto_deploy_manager is None:
        _auto_deploy_manager = AutoDeployManager()
    return _auto_deploy_manager
