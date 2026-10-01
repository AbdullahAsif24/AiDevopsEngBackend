"""OAuth + personal API-token routes for Vercel / Render / Railway."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ..auth import get_user_id
from ..config import settings
from ..services.database import get_db_service
from ..services.oauth import (
    get_vercel_oauth,
    get_render_oauth,
    get_railway_oauth,
)

router = APIRouter(prefix="/oauth", tags=["oauth"])


class OAuthUrlResponse(BaseModel):
    auth_url: str
    platform: str


class OAuthTokenResponse(BaseModel):
    platform: str
    access_token: str
    expires_at: str | None = None


class SaveTokenRequest(BaseModel):
    platform: str = Field(..., description="vercel | render | railway")
    access_token: str = Field(..., min_length=8)
    user_id: str | None = None


class PlatformConfig(BaseModel):
    oauth_configured: bool
    account_token_configured: bool
    connected: bool
    mode: str  # oauth | account_token | disconnected


def _frontend_redirect(platform: str, ok: bool, detail: str = "") -> RedirectResponse:
    base = (settings.frontend_url or "http://127.0.0.1:5173").rstrip("/")
    status = "success" if ok else "error"
    q = f"oauth={platform}&status={status}"
    if detail:
        from urllib.parse import quote

        q += f"&detail={quote(detail[:200])}"
    return RedirectResponse(url=f"{base}/?{q}", status_code=302)


@router.get("/config")
async def oauth_config(user_id: str = Depends(get_user_id)) -> dict[str, PlatformConfig]:
    """What the UI needs to render Connect buttons."""
    db = get_db_service()

    async def one(platform: str, oauth_ok: bool, account_ok: bool) -> PlatformConfig:
        cred = await db.get_user_credential(user_id, platform)
        connected = cred is not None or account_ok
        if cred is not None:
            mode = "oauth" if oauth_ok else "token"
        elif account_ok:
            mode = "account_token"
        else:
            mode = "disconnected"
        return PlatformConfig(
            oauth_configured=oauth_ok,
            account_token_configured=account_ok,
            connected=connected,
            mode=mode,
        )

    return {
        "vercel": await one(
            "vercel",
            get_vercel_oauth() is not None,
            bool(settings.vercel_api_token),
        ),
        "render": await one(
            "render",
            get_render_oauth() is not None,
            bool(settings.render_api_token),
        ),
        "railway": await one(
            "railway",
            get_railway_oauth() is not None,
            False,
        ),
    }


@router.post("/credentials/token")
async def save_personal_token(
    payload: SaveTokenRequest,
    user_id: str = Depends(get_user_id),
) -> dict:
    """Save a personal Vercel/Render API token (no OAuth app required)."""
    platform = payload.platform.lower().strip()
    if platform not in ("vercel", "render", "railway"):
        raise HTTPException(status_code=400, detail="Invalid platform")

    uid = payload.user_id or user_id
    db = get_db_service()
    await db.store_user_credential(
        user_id=uid,
        platform=platform,
        access_token=payload.access_token.strip(),
        refresh_token=None,
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=365),
    )
    return {"ok": True, "platform": platform, "connected": True, "user_id": uid}


@router.get("/vercel/authorize")
async def get_vercel_auth_url(
    user_id: str = Query("local-user"),
) -> OAuthUrlResponse:
    vercel_oauth = get_vercel_oauth()
    if not vercel_oauth:
        raise HTTPException(
            status_code=503,
            detail="Vercel OAuth app not configured. Paste a Vercel API token instead (POST /oauth/credentials/token).",
        )
    return OAuthUrlResponse(
        auth_url=vercel_oauth.get_auth_url(state=user_id), platform="vercel"
    )


@router.get("/vercel/callback")
async def vercel_callback(
    code: str = Query(...),
    state: str = Query(None),
):
    vercel_oauth = get_vercel_oauth()
    if not vercel_oauth:
        return _frontend_redirect("vercel", False, "OAuth not configured")
    try:
        user_id = state if state and state != "undefined" else "local-user"
        await vercel_oauth.exchange_code_for_token(code=code, user_id=user_id)
        return _frontend_redirect("vercel", True)
    except Exception as e:
        return _frontend_redirect("vercel", False, str(e))


@router.get("/render/authorize")
async def get_render_auth_url(
    user_id: str = Query("local-user"),
) -> OAuthUrlResponse:
    render_oauth = get_render_oauth()
    if not render_oauth:
        raise HTTPException(
            status_code=503,
            detail="Render OAuth app not configured. Paste a Render API key instead (POST /oauth/credentials/token).",
        )
    return OAuthUrlResponse(
        auth_url=render_oauth.get_auth_url(state=user_id), platform="render"
    )


@router.get("/render/callback")
async def render_callback(
    code: str = Query(...),
    state: str = Query(None),
):
    render_oauth = get_render_oauth()
    if not render_oauth:
        return _frontend_redirect("render", False, "OAuth not configured")
    try:
        user_id = state if state else "local-user"
        await render_oauth.exchange_code_for_token(code=code, user_id=user_id)
        return _frontend_redirect("render", True)
    except Exception as e:
        return _frontend_redirect("render", False, str(e))


@router.get("/railway/authorize")
async def get_railway_auth_url(
    user_id: str = Query("local-user"),
) -> OAuthUrlResponse:
    railway_oauth = get_railway_oauth()
    if not railway_oauth:
        raise HTTPException(status_code=503, detail="Railway OAuth not configured")
    return OAuthUrlResponse(
        auth_url=railway_oauth.get_auth_url(state=user_id), platform="railway"
    )


@router.get("/railway/callback")
async def railway_callback(
    code: str = Query(...),
    state: str = Query(None),
):
    railway_oauth = get_railway_oauth()
    if not railway_oauth:
        return _frontend_redirect("railway", False, "OAuth not configured")
    try:
        user_id = state if state else "local-user"
        await railway_oauth.exchange_code_for_token(code=code, user_id=user_id)
        return _frontend_redirect("railway", True)
    except Exception as e:
        return _frontend_redirect("railway", False, str(e))


@router.delete("/credentials/{platform}")
async def delete_credentials(
    platform: str, user_id: str = Depends(get_user_id)
) -> dict[str, str]:
    if platform not in ["vercel", "render", "railway"]:
        raise HTTPException(status_code=400, detail="Invalid platform")
    db = get_db_service()
    await db.delete_user_credential(user_id, platform)
    return {"message": f"Deleted {platform} credentials successfully"}


@router.get("/credentials/status")
async def get_credentials_status(user_id: str = Depends(get_user_id)) -> dict[str, dict]:
    db = get_db_service()
    status: dict[str, dict] = {}
    for platform, account_ok in (
        ("vercel", bool(settings.vercel_api_token)),
        ("render", bool(settings.render_api_token)),
        ("railway", False),
    ):
        creds = await db.get_user_credential(user_id, platform)
        status[platform] = {
            "connected": creds is not None or account_ok,
            "user_token": creds is not None,
            "account_token": account_ok,
            "expires_at": creds.get("token_expires_at") if creds else None,
        }
    return status
