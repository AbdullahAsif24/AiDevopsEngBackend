"""HTTP receiver for incoming Git webhooks (GitHub / GitLab / Generic)."""
from __future__ import annotations

import json
from fastapi import APIRouter, Header, HTTPException, Request

from ..services.auto_deploy import get_auto_deploy_manager

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/github/{config_id}")
async def receive_github_webhook(
    config_id: str,
    request: Request,
    x_github_event: str = Header("push", alias="X-GitHub-Event"),
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
) -> dict:
    """Handle incoming GitHub webhook for a specific auto-deploy configuration."""
    manager = get_auto_deploy_manager()
    config = await manager.get_config(config_id)
    if not config:
        raise HTTPException(status_code=404, detail="Webhook configuration not found")

    raw_body = await request.body()

    # Verify signature if secret configured
    if config.webhook_secret:
        valid = manager.verify_signature(raw_body, x_hub_signature_256, config.webhook_secret)
        if not valid:
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # Respond to GitHub ping event
    if x_github_event == "ping":
        return {"status": "ok", "message": "Pong! Webhook verified successfully."}

    if x_github_event != "push":
        return {"status": "ignored", "message": f"Ignoring event type '{x_github_event}'"}

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Extract branch from ref (refs/heads/main -> main)
    ref = payload.get("ref", "")
    branch = ref.replace("refs/heads/", "") if ref.startswith("refs/heads/") else ref

    repo_url = payload.get("repository", {}).get("clone_url") or payload.get("repository", {}).get("html_url") or config.repo_url
    head_commit = payload.get("head_commit") or {}
    commit_sha = head_commit.get("id", "latest")
    commit_message = head_commit.get("message", "Commit via git push")
    committer = head_commit.get("author", {}).get("username") or payload.get("pusher", {}).get("name") or "git-pusher"

    event = await manager.handle_push_event(
        config=config,
        repo_url=repo_url,
        branch=branch,
        commit_sha=commit_sha,
        commit_message=commit_message,
        committer=committer,
    )

    return {
        "status": event.status,
        "job_id": event.job_id,
        "message": f"Push event processed for {repo_url} ({branch})",
    }


@router.post("/github")
async def receive_generic_github_webhook(
    request: Request,
    x_github_event: str = Header("push", alias="X-GitHub-Event"),
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
) -> dict:
    """Handle generic GitHub webhook with dynamic repo matching."""
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if x_github_event == "ping":
        return {"status": "ok", "message": "Pong! Generic webhook reachable."}

    return {"status": "ok", "message": "Event received"}
