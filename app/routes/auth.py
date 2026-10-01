"""Authentication routes for GitHub OAuth and session management."""
from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from ..config import settings, supabase_configured
from ..services.database import get_db_service

router = APIRouter(prefix="/auth", tags=["auth"])


class GitHubAuthUrlResponse(BaseModel):
    """Response for GitHub OAuth authorization URL."""

    auth_url: str


class GitHubCallbackRequest(BaseModel):
    """Request body for GitHub OAuth callback."""

    code: str


class SessionResponse(BaseModel):
    """Response for session information."""

    user_id: str
    access_token: str
    github_username: str | None = None
    email: str | None = None
    avatar_url: str | None = None


@router.get("/github/authorize")
async def get_github_auth_url() -> GitHubAuthUrlResponse:
    """Get GitHub OAuth authorization URL.

    Users should visit this URL to authorize the application to access their GitHub account.
    """
    if not supabase_configured():
        raise HTTPException(
            status_code=503, detail="Supabase not configured - cannot perform GitHub OAuth"
        )

    # Construct GitHub OAuth URL using Supabase
    # The redirect_to should point back to the frontend so it can handle the callback
    # We use the frontend URL with the code parameter
    auth_url = f"{settings.supabase_url}/auth/v1/authorize?provider=github&redirect_to=http://localhost:5173"

    return GitHubAuthUrlResponse(auth_url=auth_url)


@router.post("/github/callback")
async def github_callback(request: GitHubCallbackRequest) -> SessionResponse:
    """Handle GitHub OAuth callback via Supabase.

    Exchange the authorization code for a session and return user information.
    """
    if not supabase_configured():
        raise HTTPException(
            status_code=503, detail="Supabase not configured"
        )

    # Exchange code for session with Supabase
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{settings.supabase_url}/auth/v1/token?grant_type=authorization_code",
            headers={
                "Content-Type": "application/json",
                "apikey": settings.supabase_service_role_key,
            },
            json={
                "code": request.code,
                "redirect_uri": "http://localhost:5173",
            },
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=400, detail=f"GitHub OAuth failed: {response.text}"
        )

    token_data = response.json()
    access_token = token_data.get("access_token")

    # Get user information using the access token
    async with httpx.AsyncClient() as client:
        user_response = await client.get(
            f"{settings.supabase_url}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "apikey": settings.supabase_service_role_key,
            },
        )

    if user_response.status_code != 200:
        raise HTTPException(
            status_code=400, detail="Failed to get user information"
        )

    user_data = user_response.json()

    # Extract GitHub-specific information
    user_metadata = user_data.get("user_metadata", {})
    identities = user_data.get("identities", [])
    github_identity = next(
        (id for id in identities if id.get("provider") == "github"), None
    )

    github_username = None
    if github_identity:
        github_username = github_identity.get("identity_data", {}).get("user_name")

    return SessionResponse(
        user_id=user_data.get("id"),
        access_token=access_token,
        github_username=github_username or user_metadata.get("user_name"),
        email=user_data.get("email"),
        avatar_url=user_metadata.get("avatar_url"),
    )


@router.get("/session")
async def get_session(request: Request) -> SessionResponse | None:
    """Get current session information from Authorization header.

    Returns the user's session if authenticated, None otherwise.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None

    token = auth_header.replace("Bearer ", "")

    if not supabase_configured():
        return None

    # Verify token with Supabase
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{settings.supabase_url}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": settings.supabase_service_role_key,
            },
        )

    if response.status_code != 200:
        return None

    user_data = response.json()

    # Extract GitHub-specific information
    user_metadata = user_data.get("user_metadata", {})
    identities = user_data.get("identities", [])
    github_identity = next(
        (id for id in identities if id.get("provider") == "github"), None
    )

    github_username = None
    if github_identity:
        github_username = github_identity.get("identity_data", {}).get("user_name")

    return SessionResponse(
        user_id=user_data.get("id"),
        access_token=token,  # Return the same token for frontend use
        github_username=github_username or user_metadata.get("user_name"),
        email=user_data.get("email"),
        avatar_url=user_metadata.get("avatar_url"),
    )


@router.post("/logout")
async def logout(request: Request) -> dict[str, str]:
    """Logout the current session.

    This is handled on the frontend by clearing the token, but we provide
    an endpoint for consistency and future server-side session management.
    """
    # For now, this is a no-op since Supabase handles token invalidation
    # In the future, we might want to add server-side session invalidation
    return {"message": "Logged out successfully"}