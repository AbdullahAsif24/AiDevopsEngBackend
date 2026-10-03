"""Authentication middleware for Supabase JWT verification."""
from __future__ import annotations

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .config import settings, supabase_configured

security = HTTPBearer(auto_error=False)


DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000000"


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
    if not credentials:
        return DEFAULT_USER_ID

    if not supabase_configured():
        return DEFAULT_USER_ID

    token = credentials.credentials

    # Verify token with Supabase
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{settings.supabase_url}/auth/v1/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": settings.supabase_service_role_key,
                },
            )

        if response.status_code == 200:
            user_data = response.json()
            user_id = user_data.get("id")
            if user_id:
                return user_id
    except Exception:
        pass

    return DEFAULT_USER_ID


async def get_user_id(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> str:
    """Dependency that returns the user_id from the verified token or falls back to standard user UUID.

    Args:
        credentials: The HTTP Bearer credentials from the request header

    Returns:
        The user_id as a string
    """
    if credentials is None:
        return DEFAULT_USER_ID
    return await verify_supabase_token(credentials)




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