"""FastAPI application entry point for the AI DevOps backend.

Wires together:
  * HTTP job routes (POST/GET /jobs)
  * WebSocket event streaming
  * CORS for the React frontend
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routes import api_jobs as api_jobs_router
from .routes import deployments as deployments_router
from .routes import jobs as jobs_router
from .routes import ws as ws_router

app = FastAPI(
    title="AI DevOps Engineer Backend",
    description=(
        "Turns a GitHub repo URL into a Dockerfile via Groq, builds/tests it with Docker, "
        "and deploys to Render with self-healing retries."
    ),
    version="0.2.0",
)

# Allow the React dev server (and any local frontend) to call us during the
# hackathon. Tighten origins before any real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(jobs_router.router)
app.include_router(api_jobs_router.router)
app.include_router(deployments_router.router)
app.include_router(ws_router.router)


@app.get("/health")
async def health() -> dict:
    """Liveness probe plus optional DevOps capability flags (no secrets)."""
    from .services.deploy import deploy_configured
    from .services.supabase_store import supabase_configured
    from .config import settings

    docker_ok = False
    if not settings.skip_docker_build:
        try:
            from .services.docker_build import _get_client

            _get_client(timeout_s=3)
            docker_ok = True
        except Exception:
            docker_ok = False

    return {
        "status": "ok",
        "docker": "skipped" if settings.skip_docker_build else ("ok" if docker_ok else "unavailable"),
        "deploy": "configured" if deploy_configured() else "not_configured",
        "supabase": "configured" if supabase_configured() else "not_configured",
    }
