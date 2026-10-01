"""Authentication middleware for Supabase JWT verification."""
from __future__ import annotations

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .config import settings, supabase_configured

security = HTTPBearer()


async def verify_supabase_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """Verify Supabase JWT and return user_id.

    This function validates the JWT token with Supabase and extracts the user_id.
    Raises HTTPException if the token is invalid or expired.

    Args:
        credentials: The HTTP Bearer credentials from the request header

    Returns:
        The user_id (UUID) from the validated token

    Raises:
        HTTPException: If token is invalid, expired, or Supabase verification fails
    """
    if not supabase_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.",
        )

    token = credentials.credentials

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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user_data = response.json()
    user_id = user_data.get("id")

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to extract user_id from token",
        )

    return user_id


async def get_user_id(user_id: str = Depends(verify_supabase_token)) -> str:
    """Dependency that returns the user_id from the verified token.

    This is a convenience wrapper around verify_supabase_token for use in route handlers.

    Args:
        user_id: The user_id from verify_supabase_token

    Returns:
        The user_id (UUID) as a string
    """
    return user_id


async def optional_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
) -> str | None:
    """Optional authentication that returns user_id if token is provided, None otherwise.

    This allows routes to work both with and without authentication, useful for
    backwards compatibility during migration.

    Args:
        credentials: Optional HTTP Bearer credentials

    Returns:
        The user_id if token is valid, None if no token provided
    """
    if credentials is None:
        return None

    try:
        return await verify_supabase_token(credentials)
    except HTTPException:
        return None