"""FastAPI application entry point for the AI DevOps backend.

Wires together:
  * HTTP job routes (POST/GET /jobs)
  * WebSocket event streaming
  * CORS for the React frontend
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routes import jobs as jobs_router
from .routes import detection as detection_router
from .routes import deployments as deployments_router
from .routes import ws as ws_router
from .routes import oauth as oauth_router
from .routes import env_vars as env_vars_router
from .routes import auth as auth_router

app = FastAPI(
    title="AI DevOps Engineer Backend",
    description="Turns a GitHub repo URL into a Dockerfile via Groq, deploys to Vercel/Render with intelligent detection.",
    version="0.3.0",
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
app.include_router(detection_router.router)
app.include_router(deployments_router.router)
app.include_router(ws_router.router)
app.include_router(oauth_router.router)
app.include_router(env_vars_router.router)
app.include_router(auth_router.router)


@app.get("/health")
async def health() -> dict:
    """Liveness probe plus optional deployment capability flags (no secrets)."""
    from .config import settings, supabase_configured

    return {
        "status": "ok",
        "version": "0.3.0",
        "supabase": "configured" if supabase_configured() else "not_configured",
        "deployment_mode": "live",
    }
