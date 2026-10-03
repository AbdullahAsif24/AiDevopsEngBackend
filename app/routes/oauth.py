"""OAuth routes for Vercel and Render authentication."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ..auth import get_user_id
from ..services.oauth import VercelOAuth, RenderOAuth, RailwayOAuth, get_vercel_oauth, get_render_oauth, get_railway_oauth

router = APIRouter(prefix="/oauth", tags=["oauth"])


class OAuthCallbackRequest(BaseModel):
    """Request body for OAuth callback."""

    code: str
    state: str | None = None


class OAuthUrlResponse(BaseModel):
    """Response for OAuth authorization URL."""

    auth_url: str
    platform: str


class OAuthTokenResponse(BaseModel):
    """Response for successful token exchange."""

    platform: str
    access_token: str
    expires_at: str | None = None


@router.get("/vercel/authorize")
async def get_vercel_auth_url(
    user_id: str = Query(..., description="User ID for state parameter"),
) -> OAuthUrlResponse:
    """Get Vercel OAuth authorization URL.

    Users should visit this URL to authorize the application to access their Vercel account.
    """
    vercel_oauth = get_vercel_oauth()
    if not vercel_oauth:
        raise HTTPException(
            status_code=503, detail="Vercel OAuth not configured on server"
        )

    auth_url = vercel_oauth.get_auth_url(state=user_id)
    return OAuthUrlResponse(auth_url=auth_url, platform="vercel")


@router.get("/vercel/callback")
async def vercel_callback(
    code: str = Query(..., description="Authorization code"),
    state: str = Query(None, description="State parameter"),
    request: Request = None
) -> OAuthTokenResponse:
    """Handle Vercel OAuth callback.

    Exchange the authorization code for an access token and store it in the database.
    """
    vercel_oauth = get_vercel_oauth()
    if not vercel_oauth:
        raise HTTPException(
            status_code=503, detail="Vercel OAuth not configured on server"
        )

    try:
        # Try to get user_id from state, or use a default for testing
        user_id = state if state and state != "undefined" else "test_user"
        print(f"Vercel callback - using user_id: {user_id}")
        token_data = await vercel_oauth.exchange_code_for_token(
            code=code, user_id=user_id
        )
        
        # Redirect back to frontend with success
        return OAuthTokenResponse(
            platform="vercel",
            access_token=token_data["access_token"],
            expires_at=token_data.get("expires_at"),
        )
    except Exception as e:
        print(f"Vercel OAuth error: {e}")
        raise HTTPException(status_code=400, detail=f"Vercel OAuth failed: {str(e)}")


@router.get("/render/authorize")
async def get_render_auth_url(
    user_id: str = Query(..., description="User ID for state parameter"),
) -> OAuthUrlResponse:
    """Get Render OAuth authorization URL.

    Users should visit this URL to authorize the application to access their Render account.
    """
    render_oauth = get_render_oauth()
    if not render_oauth:
        raise HTTPException(
            status_code=503, detail="Render OAuth not configured on server"
        )

    auth_url = render_oauth.get_auth_url(state=user_id)
    return OAuthUrlResponse(auth_url=auth_url, platform="render")


@router.get("/render/callback")
async def render_callback(
    code: str = Query(..., description="Authorization code"),
    state: str = Query(None, description="State parameter")
) -> OAuthTokenResponse:
    """Handle Render OAuth callback.

    Exchange the authorization code for an access token and store it in the database.
    """
    render_oauth = get_render_oauth()
    if not render_oauth:
        raise HTTPException(
            status_code=503, detail="Render OAuth not configured on server"
        )

    try:
        # Use state as user_id if provided, otherwise use a default
        user_id = state if state else "default_user"
        token_data = await render_oauth.exchange_code_for_token(
            code=code, user_id=user_id
        )
        return OAuthTokenResponse(
            platform="render",
            access_token=token_data["access_token"],
            expires_at=token_data.get("expires_at"),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Render OAuth failed: {str(e)}")


@router.get("/railway/authorize")
async def get_railway_auth_url(
    user_id: str = Query(..., description="User ID for state parameter"),
) -> OAuthUrlResponse:
    """Get Railway OAuth authorization URL.

    Users should visit this URL to authorize the application to access their Railway account.
    """
    railway_oauth = get_railway_oauth()
    if not railway_oauth:
        raise HTTPException(
            status_code=503, detail="Railway OAuth not configured on server"
        )

    auth_url = railway_oauth.get_auth_url(state=user_id)
    return OAuthUrlResponse(auth_url=auth_url, platform="railway")


@router.get("/railway/callback")
async def railway_callback(
    code: str = Query(..., description="Authorization code"),
    state: str = Query(None, description="State parameter")
) -> OAuthTokenResponse:
    """Handle Railway OAuth callback.

    Exchange the authorization code for an access token and store it in the database.
    """
    railway_oauth = get_railway_oauth()
    if not railway_oauth:
        raise HTTPException(
            status_code=503, detail="Railway OAuth not configured on server"
        )

    try:
        # Use state as user_id if provided, otherwise use a default
        user_id = state if state else "default_user"
        token_data = await railway_oauth.exchange_code_for_token(
            code=code, user_id=user_id
        )
        return OAuthTokenResponse(
            platform="railway",
            access_token=token_data["access_token"],
            expires_at=token_data.get("expires_at"),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Railway OAuth failed: {str(e)}")


@router.delete("/credentials/{platform}")
async def delete_credentials(
    platform: str, user_id: str = Depends(get_user_id)
) -> dict[str, str]:
    """Delete stored OAuth credentials for a platform.

    Args:
        platform: Either 'vercel', 'render', or 'railway'
        user_id: The authenticated user's ID

    Returns:
        Success message
    """
    if platform not in ["vercel", "render", "railway"]:
        raise HTTPException(
            status_code=400, detail="Platform must be either 'vercel', 'render', or 'railway'"
        )

    from ..services.database import get_db_service

    db = get_db_service()
    await db.delete_user_credential(user_id, platform)

    return {"message": f"Deleted {platform} credentials successfully"}


@router.get("/credentials/status")
async def get_credentials_status(user_id: str = Depends(get_user_id)) -> dict[str, dict]:
    """Get the status of OAuth credentials for all platforms.

    Returns information about which platforms the user has connected.
    """
    from ..services.database import get_db_service

    db = get_db_service()

    status = {}

    # Check Vercel credentials
    vercel_creds = await db.get_user_credential(user_id, "vercel")
    status["vercel"] = {
        "connected": vercel_creds is not None,
        "expires_at": vercel_creds.get("token_expires_at") if vercel_creds else None,
    }

    # Check Render credentials
    render_creds = await db.get_user_credential(user_id, "render")
    status["render"] = {
        "connected": render_creds is not None,
        "expires_at": render_creds.get("token_expires_at") if render_creds else None,
    }

    # Check Railway credentials
    railway_creds = await db.get_user_credential(user_id, "railway")
    status["railway"] = {
        "connected": railway_creds is not None,
        "expires_at": railway_creds.get("token_expires_at") if railway_creds else None,
    }

    return status