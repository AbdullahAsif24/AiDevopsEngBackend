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
from .routes import ws as ws_router

app = FastAPI(
    title="AI DevOps Engineer Backend",
    description="Turns a GitHub repo URL into a Dockerfile via Groq, with a self-healing retry loop.",
    version="0.1.0",
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
app.include_router(ws_router.router)


@app.get("/health")
async def health() -> dict:
    """Simple liveness probe."""
    return {"status": "ok"}
