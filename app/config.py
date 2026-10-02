# Configuration for the AI DevOps backend.
#
# Everything is read from the environment (optionally seeded from a .env file
# via python-dotenv). No secrets are ever hardcoded or committed.
import os

from dotenv import load_dotenv

# Load a local .env file if present (ignored by git). This makes local dev painless
# while the same code reads from real env vars in production.
load_dotenv()

# Debug environment variable loading
print(f"Config loaded - Vercel Client ID: {os.getenv('VERCEL_CLIENT_ID', 'None')[:10] if os.getenv('VERCEL_CLIENT_ID') else 'None'}...")
print(f"Supabase URL: {os.getenv('SUPABASE_URL', 'None')[:20] if os.getenv('SUPABASE_URL') else 'None'}...")
print(f"Vercel API Token: {os.getenv('VERCEL_API_TOKEN', 'None')[:10] if os.getenv('VERCEL_API_TOKEN') else 'None'}...")
_render_tok = os.getenv("RENDER_API_TOKEN") or os.getenv("RENDER_API_KEY") or ""
print(f"Render API Token: {_render_tok[:10] if _render_tok else 'None'}...")


class Settings:
    """Central place for tunable knobs. All values fall back to sane defaults."""

    # Groq API key. Ideally set as GROQ_API_KEY in the environment.
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")

    # Fast Groq-hosted model. openai/gpt-oss-120b is a good speed/quality
    # balance for one-shot structured outputs.
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

    # Temperature kept low so the model sticks to the template rather than
    # inventing Dockerfile syntax.
    groq_temperature: float = float(os.getenv("GROQ_TEMPERATURE", "0.1"))

    # How many times we let Groq patch the Dockerfile after a failed build.
    # 3 retries (plus the initial generation) is plenty for a hackathon.
    max_heal_retries: int = int(os.getenv("MAX_HEAL_RETRIES", "3"))

    # Supabase configuration for authentication and database
    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_service_role_key: str = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        or os.getenv("SUPABASE_SERVICE_KEY", "")
    )

    # OAuth callback URLs (defaults for local hackathon)
    vercel_oauth_callback_url: str = os.getenv(
        "VERCEL_OAUTH_CALLBACK_URL", "http://127.0.0.1:8000/oauth/vercel/callback"
    )
    render_oauth_callback_url: str = os.getenv(
        "RENDER_OAUTH_CALLBACK_URL", "http://127.0.0.1:8000/oauth/render/callback"
    )
    railway_oauth_callback_url: str = os.getenv(
        "RAILWAY_OAUTH_CALLBACK_URL", "http://127.0.0.1:8000/oauth/railway/callback"
    )
    frontend_url: str = os.getenv("FRONTEND_URL", "http://127.0.0.1:5173")

    # OAuth configuration
    vercel_client_id: str = os.getenv("VERCEL_CLIENT_ID", "")
    vercel_client_secret: str = os.getenv("VERCEL_CLIENT_SECRET", "")
    render_client_id: str = os.getenv("RENDER_CLIENT_ID", "")
    render_client_secret: str = os.getenv("RENDER_CLIENT_SECRET", "")
    railway_client_id: str = os.getenv("RAILWAY_CLIENT_ID", "")
    railway_client_secret: str = os.getenv("RAILWAY_CLIENT_SECRET", "")

    # Account-level API tokens (fallback for deployments / "connect" without OAuth app)
    vercel_api_token: str = os.getenv("VERCEL_API_TOKEN", "")
    render_api_token: str = (
        os.getenv("RENDER_API_TOKEN", "") or os.getenv("RENDER_API_KEY", "")
    )
    render_owner_id: str = os.getenv("RENDER_OWNER_ID", "")
    vercel_team_id: str = os.getenv("VERCEL_TEAM_ID", "")

    # GitHub token for committing Dockerfiles to repos (fallback if OAuth fails)
    github_token: str = os.getenv("GITHUB_TOKEN", "")


settings = Settings()


def supabase_configured() -> bool:
    """Check if Supabase is properly configured."""
    return bool(settings.supabase_url and settings.supabase_service_role_key)
