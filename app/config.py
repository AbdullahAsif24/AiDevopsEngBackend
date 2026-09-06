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

    # Fast Groq-hosted model. llama-3.3-70b-versatile is a good speed/quality
    # balance for one-shot structured outputs.
    groq_model: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    # Temperature kept low so the model sticks to the template rather than
    # inventing Dockerfile syntax.
    groq_temperature: float = float(os.getenv("GROQ_TEMPERATURE", "0.1"))

    # How many times we let Groq patch the Dockerfile after a failed build.
    # 3 retries (plus the initial generation) is plenty for a hackathon.
    max_heal_retries: int = int(os.getenv("MAX_HEAL_RETRIES", "3"))

    # Vercel deployment configuration
    vercel_api_token: str = os.getenv("VERCEL_API_TOKEN", "")
    vercel_team_id: str = os.getenv("VERCEL_TEAM_ID", "")  # Optional: for team deployments

    # Render deployment configuration
    render_api_key: str = os.getenv("RENDER_API_KEY", "")


settings = Settings()


def vercel_configured() -> bool:
    """Check if Vercel deployment is properly configured."""
    return bool(settings.vercel_api_token)


def render_configured() -> bool:
    """Check if Render deployment is properly configured."""
    return bool(settings.render_api_key)
