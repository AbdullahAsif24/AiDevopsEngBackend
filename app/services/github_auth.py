"""GitHub authentication service for private repository access."""
from __future__ import annotations

import httpx

from ..config import settings, supabase_configured


async def get_user_github_token(user_id: str) -> str | None:
    """Get GitHub token from Supabase user metadata or environment variable.

    When users authenticate with GitHub OAuth via Supabase, their GitHub access token
    is stored in the user's metadata. This function retrieves it.
    Falls back to GITHUB_TOKEN environment variable if OAuth fails.

    Args:
        user_id: The Supabase user ID

    Returns:
        The GitHub access token if available, None otherwise
    """
    # First try to get from environment variable (fallback)
    if settings.github_token:
        return settings.github_token

    if not supabase_configured():
        return None

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{settings.supabase_url}/auth/v1/admin/users/{user_id}",
            headers={
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
                "apikey": settings.supabase_service_role_key,
            },
        )

    if response.status_code != 200:
        return None

    user_data = response.json()
    # GitHub token is typically stored in user metadata after OAuth
    # The exact structure depends on Supabase OAuth configuration
    user_metadata = user_data.get("user_metadata", {})

    # Try common locations where GitHub token might be stored
    github_token = (
        user_metadata.get("github_token")
        or user_metadata.get("provider_token")
        or user_metadata.get("access_token")
    )

    return github_token


async def validate_github_token(token: str) -> bool:
    """Validate that a GitHub token is functional.

    Args:
        token: The GitHub access token to validate

    Returns:
        True if token is valid, False otherwise
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"token {token}"},
        )

    return response.status_code == 200