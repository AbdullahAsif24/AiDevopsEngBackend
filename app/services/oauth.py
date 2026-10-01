"""OAuth services for Vercel and Render deployment platforms."""
from __future__ import annotations

import httpx
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlencode

from ..config import settings
from .database import get_db_service


class OAuthError(Exception):
    """Raised when OAuth operations fail."""


class VercelOAuth:
    """Vercel OAuth integration for user-specific deployments."""

    def __init__(self):
        """Initialize Vercel OAuth service."""
        print(f"Vercel OAuth initialization - Client ID: {settings.vercel_client_id[:10] if settings.vercel_client_id else 'None'}...")
        if not settings.vercel_client_id or not settings.vercel_client_secret:
            raise OAuthError("Vercel OAuth not configured")

    def get_auth_url(self, state: str = "default") -> str:
        """Generate Vercel OAuth authorization URL.

        Args:
            state: OAuth state parameter for CSRF protection

        Returns:
            The authorization URL for user to visit
        """
        params = {
            "client_id": settings.vercel_client_id,
            "redirect_uri": settings.vercel_oauth_callback_url,
            "response_type": "code",
            "scope": "user",  # Vercel OAuth scope
            "state": state,
        }

        base_url = "https://vercel.com/oauth/authorize"
        return f"{base_url}?{urlencode(params)}"

    async def exchange_code_for_token(
        self, code: str, user_id: str
    ) -> dict[str, str]:
        """Exchange authorization code for access token.

        Args:
            code: The authorization code from callback
            user_id: The user ID to associate with the token

        Returns:
            Dictionary containing token information

        Raises:
            OAuthError: If token exchange fails
        """
        data = {
            "client_id": settings.vercel_client_id,
            "client_secret": settings.vercel_client_secret,
            "code": code,
            "redirect_uri": settings.vercel_oauth_callback_url,
        }

        print(f"Vercel token exchange - Client ID: {settings.vercel_client_id}")
        print(f"Vercel token exchange - Redirect URI: {settings.vercel_oauth_callback_url}")
        print(f"Vercel token exchange - Code: {code[:10]}...")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.vercel.com/v2/oauth/access_token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        print(f"Vercel API response status: {response.status_code}")
        print(f"Vercel API response: {response.text}")

        if response.status_code != 200:
            raise OAuthError(f"Vercel token exchange failed: {response.text}")

        token_data = response.json()

        # Store token in database
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in", 86400)  # Default 24 hours

        token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in
        )

        db = get_db_service()
        await db.store_user_credential(
            user_id=user_id,
            platform="vercel",
            access_token=access_token,
            refresh_token=refresh_token,
            token_expires_at=token_expires_at,
        )

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": token_expires_at.isoformat(),
        }

    async def get_user_token(self, user_id: str) -> str:
        """Get user's Vercel access token, refreshing if necessary.

        Args:
            user_id: The user ID

        Returns:
            The access token

        Raises:
            OAuthError: If token retrieval fails
        """
        db = get_db_service()
        credential = await db.get_user_credential(user_id, "vercel")

        if not credential:
            raise OAuthError("No Vercel credentials found for user")

        access_token = credential.get("access_token")
        token_expires_at = credential.get("token_expires_at")

        # Check if token needs refresh
        if token_expires_at:
            expires_at = datetime.fromisoformat(token_expires_at)
            if expires_at < datetime.now(timezone.utc) + timedelta(minutes=5):
                # Token expired or expiring soon, refresh it
                return await self._refresh_token(user_id, credential.get("refresh_token"))

        return access_token

    async def _refresh_token(self, user_id: str, refresh_token: str) -> str:
        """Refresh an expired access token.

        Args:
            user_id: The user ID
            refresh_token: The refresh token

        Returns:
            The new access token

        Raises:
            OAuthError: If refresh fails
        """
        data = {
            "client_id": settings.vercel_client_id,
            "client_secret": settings.vercel_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.vercel.com/v2/oauth/access_token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            raise OAuthError(f"Vercel token refresh failed: {response.text}")

        token_data = response.json()

        # Update stored token
        access_token = token_data.get("access_token")
        new_refresh_token = token_data.get("refresh_token", refresh_token)
        expires_in = token_data.get("expires_in", 86400)

        token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in
        )

        db = get_db_service()
        await db.store_user_credential(
            user_id=user_id,
            platform="vercel",
            access_token=access_token,
            refresh_token=new_refresh_token,
            token_expires_at=token_expires_at,
        )

        return access_token


class RenderOAuth:
    """Render OAuth integration for user-specific deployments."""

    def __init__(self):
        """Initialize Render OAuth service."""
        if not settings.render_client_id or not settings.render_client_secret:
            raise OAuthError("Render OAuth not configured")

    def get_auth_url(self, state: str = "default") -> str:
        """Generate Render OAuth authorization URL.

        Args:
            state: OAuth state parameter for CSRF protection

        Returns:
            The authorization URL for user to visit
        """
        params = {
            "client_id": settings.render_client_id,
            "redirect_uri": settings.render_oauth_callback_url,
            "response_type": "code",
            "scope": "profile",  # Render OAuth scope
            "state": state,
        }

        base_url = "https://id.render.com/oauth2/authorize"
        return f"{base_url}?{urlencode(params)}"

    async def exchange_code_for_token(
        self, code: str, user_id: str
    ) -> dict[str, str]:
        """Exchange authorization code for access token.

        Args:
            code: The authorization code from callback
            user_id: The user ID to associate with the token

        Returns:
            Dictionary containing token information

        Raises:
            OAuthError: If token exchange fails
        """
        data = {
            "client_id": settings.render_client_id,
            "client_secret": settings.render_client_secret,
            "code": code,
            "redirect_uri": settings.render_oauth_callback_url,
            "grant_type": "authorization_code",
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://id.render.com/oauth2/token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            raise OAuthError(f"Render token exchange failed: {response.text}")

        token_data = response.json()

        # Store token in database
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in", 86400)  # Default 24 hours

        token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in
        )

        db = get_db_service()
        await db.store_user_credential(
            user_id=user_id,
            platform="render",
            access_token=access_token,
            refresh_token=refresh_token,
            token_expires_at=token_expires_at,
        )

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": token_expires_at.isoformat(),
        }

    async def get_user_token(self, user_id: str) -> str:
        """Get user's Render access token, refreshing if necessary.

        Args:
            user_id: The user ID

        Returns:
            The access token

        Raises:
            OAuthError: If token retrieval fails
        """
        db = get_db_service()
        credential = await db.get_user_credential(user_id, "render")

        if not credential:
            raise OAuthError("No Render credentials found for user")

        access_token = credential.get("access_token")
        token_expires_at = credential.get("token_expires_at")

        # Check if token needs refresh
        if token_expires_at:
            expires_at = datetime.fromisoformat(token_expires_at)
            if expires_at < datetime.now(timezone.utc) + timedelta(minutes=5):
                # Token expired or expiring soon, refresh it
                return await self._refresh_token(user_id, credential.get("refresh_token"))

        return access_token

    async def _refresh_token(self, user_id: str, refresh_token: str) -> str:
        """Refresh an expired access token.

        Args:
            user_id: The user ID
            refresh_token: The refresh token

        Returns:
            The new access token

        Raises:
            OAuthError: If refresh fails
        """
        data = {
            "client_id": settings.render_client_id,
            "client_secret": settings.render_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://id.render.com/oauth2/token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            raise OAuthError(f"Render token refresh failed: {response.text}")

        token_data = response.json()

        # Update stored token
        access_token = token_data.get("access_token")
        new_refresh_token = token_data.get("refresh_token", refresh_token)
        expires_in = token_data.get("expires_in", 86400)

        token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in
        )

        db = get_db_service()
        await db.store_user_credential(
            user_id=user_id,
            platform="render",
            access_token=access_token,
            refresh_token=new_refresh_token,
            token_expires_at=token_expires_at,
        )

        return access_token


def get_vercel_oauth() -> VercelOAuth | None:
    """Get or create Vercel OAuth service instance."""
    try:
        return VercelOAuth()
    except OAuthError:
        return None


def get_render_oauth() -> RenderOAuth | None:
    """Get or create Render OAuth service instance."""
    try:
        return RenderOAuth()
    except OAuthError:
        return None


class RailwayOAuth:
    """Railway OAuth integration for user-specific deployments."""

    def __init__(self):
        """Initialize Railway OAuth service."""
        if not settings.railway_client_id or not settings.railway_client_secret:
            raise OAuthError("Railway OAuth not configured")

    def get_auth_url(self, state: str = "default") -> str:
        """Generate Railway OAuth authorization URL.

        Args:
            state: OAuth state parameter for CSRF protection

        Returns:
            The authorization URL for user to visit
        """
        params = {
            "client_id": settings.railway_client_id,
            "redirect_uri": settings.railway_oauth_callback_url,
            "response_type": "code",
            "scope": "read:team write:team",  # Railway OAuth scope
            "state": state,
        }

        base_url = "https://backboard.railway.app/oauth2/authorize"
        return f"{base_url}?{urlencode(params)}"

    async def exchange_code_for_token(
        self, code: str, user_id: str
    ) -> dict[str, str]:
        """Exchange authorization code for access token.

        Args:
            code: The authorization code from callback
            user_id: The user ID to associate with the token

        Returns:
            Dictionary containing token information

        Raises:
            OAuthError: If token exchange fails
        """
        data = {
            "client_id": settings.railway_client_id,
            "client_secret": settings.railway_client_secret,
            "code": code,
            "redirect_uri": settings.railway_oauth_callback_url,
            "grant_type": "authorization_code",
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://backboard.railway.app/oauth2/token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            raise OAuthError(f"Railway token exchange failed: {response.text}")

        token_data = response.json()

        # Store token in database
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in", 86400)  # Default 24 hours

        token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in
        )

        db = get_db_service()
        await db.store_user_credential(
            user_id=user_id,
            platform="railway",
            access_token=access_token,
            refresh_token=refresh_token,
            token_expires_at=token_expires_at,
        )

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": token_expires_at.isoformat(),
        }

    async def get_user_token(self, user_id: str) -> str:
        """Get user's Railway access token, refreshing if necessary.

        Args:
            user_id: The user ID

        Returns:
            The access token

        Raises:
            OAuthError: If token retrieval fails
        """
        db = get_db_service()
        credential = await db.get_user_credential(user_id, "railway")

        if not credential:
            raise OAuthError("No Railway credentials found for user")

        access_token = credential.get("access_token")
        token_expires_at = credential.get("token_expires_at")

        # Check if token needs refresh
        if token_expires_at:
            expires_at = datetime.fromisoformat(token_expires_at)
            if expires_at < datetime.now(timezone.utc) + timedelta(minutes=5):
                # Token expired or expiring soon, refresh it
                return await self._refresh_token(user_id, credential.get("refresh_token"))

        return access_token

    async def _refresh_token(self, user_id: str, refresh_token: str) -> str:
        """Refresh an expired access token.

        Args:
            user_id: The user ID
            refresh_token: The refresh token

        Returns:
            The new access token

        Raises:
            OAuthError: If refresh fails
        """
        data = {
            "client_id": settings.railway_client_id,
            "client_secret": settings.railway_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://backboard.railway.app/oauth2/token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            raise OAuthError(f"Railway token refresh failed: {response.text}")

        token_data = response.json()

        # Update stored token
        access_token = token_data.get("access_token")
        new_refresh_token = token_data.get("refresh_token", refresh_token)
        expires_in = token_data.get("expires_in", 86400)

        token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in
        )

        db = get_db_service()
        await db.store_user_credential(
            user_id=user_id,
            platform="railway",
            access_token=access_token,
            refresh_token=new_refresh_token,
            token_expires_at=token_expires_at,
        )

        return access_token


def get_railway_oauth() -> RailwayOAuth | None:
    """Get or create Railway OAuth service instance."""
    try:
        return RailwayOAuth()
    except OAuthError:
        return None