# Configuration for the AI DevOps backend.
#
# Everything is read from the environment (optionally seeded from a .env file
# via python-dotenv). No secrets are ever hardcoded or committed.
import os

from dotenv import load_dotenv

# Load a local .env file if present (ignored by git). This makes local dev painless
# while the same code reads from real env vars in production.
load_dotenv()


class Settings:
    """Central place for tunable knobs. All values fall back to sane defaults."""

    # Groq API key. Ideally set as GROQ_API_KEY in the environment.
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")

    # Fast Groq-hosted model. Default updated for current Groq catalog.
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

    # Temperature kept low so the model sticks to the template rather than
    # inventing Dockerfile syntax.
    groq_temperature: float = float(os.getenv("GROQ_TEMPERATURE", "0.1"))

    # How many times we let Groq patch the Dockerfile after a failed build.
    # 3 retries (plus the initial generation) is plenty for a hackathon.
    max_heal_retries: int = int(os.getenv("MAX_HEAL_RETRIES", "3"))

    # ---- DevOps / Infra -------------------------------------------------
    # Skip the real docker build (useful on machines without Docker Desktop).
    skip_docker_build: bool = os.getenv("SKIP_DOCKER_BUILD", "").lower() in (
        "1",
        "true",
        "yes",
    )

    # Bound a single build+health attempt (seconds).
    docker_build_timeout_s: float = float(os.getenv("DOCKER_BUILD_TIMEOUT_S", "180"))
    docker_health_timeout_s: float = float(os.getenv("DOCKER_HEALTH_TIMEOUT_S", "45"))

    # Default container listen port when the generated Dockerfile doesn't say.
    default_app_port: int = int(os.getenv("DEFAULT_APP_PORT", "8000"))

    # Baked into Vite/React frontend images at `docker build` time (build-args).
    # Set VITE_API_BASE_URL to your *public* backend URL before deploying a SPA.
    vite_api_base_url: str = os.getenv("VITE_API_BASE_URL", "")
    vite_supabase_url: str = os.getenv("VITE_SUPABASE_URL", "") or os.getenv("SUPABASE_URL", "")
    vite_supabase_anon_key: str = os.getenv("VITE_SUPABASE_ANON_KEY", "")
    vite_use_mock: str = os.getenv("VITE_USE_MOCK", "false")

    # Render (primary free host).
    render_api_key: str = os.getenv("RENDER_API_KEY", "")
    render_owner_id: str = os.getenv("RENDER_OWNER_ID", "")
    render_region: str = os.getenv("RENDER_REGION", "oregon")
    render_plan: str = os.getenv("RENDER_PLAN", "free")

    # Docker Hub — used to push the local image so Render can pull it.
    dockerhub_username: str = os.getenv("DOCKERHUB_USERNAME", "")
    dockerhub_token: str = os.getenv("DOCKERHUB_TOKEN", "")

    # Supabase persistence (optional — in-memory store still works without it).
    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_service_key: str = os.getenv("SUPABASE_SERVICE_KEY", "")


settings = Settings()
