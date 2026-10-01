"""
Kite Connect authentication & account routes.

Owns the OAuth redirect flow (/kite/login -> /kite/callback), session
status/logout, and the two account-info reads (profile, margins) that
depend directly on the active Kite session.

REMOVED vs. the old backend/main.py:
  - POST /api/login-url and POST /api/login: dead duplicates of the real
    OAuth flow. Both simply called kite_login() and ignored their request
    body. The frontend (frontend/src/api/auth.js) never calls them — it
    only uses GET /kite/login, GET /kite/status, POST /kite/logout.
  - GET /api/status: explicitly marked "Legacy/compatibility wrapper" in
    its own docstring; superseded by GET /kite/status. Also unused by the
    frontend.
"""

import logging
import os
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from kiteconnect import KiteConnect

from backend.security import verify_shared_secret
from broker.kite_adapter import (
    clear_session,
    get_active_kite,
    get_active_kite_with_diagnostics,
    get_saved_session,
    save_session,
)
from config.settings import settings

logger = logging.getLogger("backend_api.kite")

router = APIRouter()


@router.get("/kite/login")
def kite_login():
    """Returns official Kite login URL using server environment API key."""
    api_key = settings.kite_api_key or os.getenv("KITE_API_KEY")
    if not api_key or api_key == "your_api_key_here":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="KITE_API_KEY is not configured in environment variables.",
        )
    kite = KiteConnect(api_key=api_key.strip())
    return {"login_url": kite.login_url()}


@router.get("/kite/callback")
def kite_callback(
    request_token: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    # Renamed from `status` (the Kite redirect's literal query param name) to
    # `auth_status` so it doesn't shadow fastapi's `status` module, which the
    # error branches below rely on. The query string the browser sends is
    # unaffected — `alias="status"` keeps binding to `?status=...`.
    auth_status: Optional[str] = Query(None, alias="status"),
):
    """
    Automatic Zerodha OAuth redirect callback endpoint.
    Exchanges request_token for access_token, persists session token,
    updates active Kite client, and redirects user back to the frontend.
    """
    frontend_url = os.getenv("FRONTEND_URL", "http://127.0.0.1:5173")

    if auth_status != "success" or not request_token:
        logger.error("Kite callback rejected: missing or invalid callback parameters")
        return RedirectResponse(
            url=f"{frontend_url}?auth_error=Kite+login+was+cancelled+or+failed.",
            status_code=307,
        )

    api_key = settings.kite_api_key or os.getenv("KITE_API_KEY")
    api_secret = settings.kite_api_secret or os.getenv("KITE_API_SECRET")

    if not api_key or not api_secret or api_key == "your_api_key_here":
        logger.error("Missing KITE_API_KEY or KITE_API_SECRET in environment.")
        return RedirectResponse(
            url=f"{frontend_url}?auth_error=Backend+missing+KITE_API_KEY+or+KITE_API_SECRET.",
            status_code=307,
        )

    try:
        kite = KiteConnect(api_key=api_key.strip())
        session_data = kite.generate_session(
            request_token=request_token.strip(),
            api_secret=api_secret.strip(),
        )

        save_session(
            api_key=api_key.strip(),
            access_token=session_data["access_token"],
            user_id=session_data.get("user_id", ""),
            user_name=session_data.get("user_name", "Trader"),
            public_token=session_data.get("public_token", ""),
        )

        logger.info(
            f"Successfully authenticated Kite session for user "
            f"{session_data.get('user_id', '')} ({session_data.get('user_name', 'Trader')})."
        )
        return RedirectResponse(url=frontend_url, status_code=307)

    except Exception:
        logger.exception("Failed to generate Kite session token")
        return RedirectResponse(
            url=f"{frontend_url}?auth_error=Authentication+failed.",
            status_code=307,
        )


@router.get("/kite/status")
def kite_status():
    """Returns Kite authentication state and user details for the frontend."""
    session = get_saved_session()
    if not session:
        return {"connected": False, "message": "Kite Not Connected"}

    kite, err = get_active_kite_with_diagnostics(force_validate=True)
    if not kite:
        return {
            "connected": False,
            "message": err or "Kite session invalid or expired. Please connect Kite.",
        }

    profile = None
    try:
        profile = kite.profile()
    except Exception:
        pass

    user_id = profile.get("user_id") if profile else session.get("user_id", "")
    user_name = profile.get("user_name") if profile else session.get("user_name", "Trader")
    products = profile.get("products", ["CNC", "NRML", "MIS", "BO", "CO"]) if profile else []
    exchanges = profile.get("exchanges", ["NSE", "BSE", "NFO", "BFO", "CDS", "MCX"]) if profile else []

    return {
        "connected": True,
        "user_id": user_id,
        "user_name": user_name,
        "products": products,
        "exchanges": exchanges,
    }


@router.post("/kite/logout")
def kite_logout():
    """Clears local access token and session state."""
    clear_session()
    return {"success": True, "message": "Logged out successfully"}


@router.get("/api/profile")
def get_user_profile():
    """Fetches user profile details."""
    kite = get_active_kite()
    if not kite:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated or session expired. Please log in.",
        )

    try:
        profile = kite.profile()
        return {
            "user_name": profile.get("user_name", "N/A"),
            "user_id": profile.get("user_id", "N/A"),
            "email": profile.get("email", "N/A"),
            "broker": profile.get("broker", "ZERODHA"),
            "user_type": profile.get("user_type", "individual"),
            "products": profile.get("products", ["CNC", "NRML", "MIS", "BO", "CO"]),
            "exchanges": profile.get("exchanges", ["NSE", "BSE", "NFO", "BFO", "CDS", "MCX"]),
            "order_types": profile.get("order_types", ["MARKET", "LIMIT", "SL", "SL-M"]),
            "avatar_url": profile.get("avatar_url", None),
        }
    except Exception as e:
        logger.error(f"Error fetching profile: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not fetch profile from Zerodha: {str(e)}",
        )


@router.get("/api/margins")
def get_user_margins():
    """Fetches equity and commodity account margins."""
    kite = get_active_kite()
    if not kite:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
        )

    try:
        margins = kite.margins()
        return margins
    except Exception as e:
        logger.error(f"Error fetching margins: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not fetch margins: {str(e)}",
        )


@router.post("/api/logout")
def logout(x_shared_secret: Optional[str] = Header(None, alias="X-Shared-Secret")):
    verify_shared_secret(x_shared_secret)
    return kite_logout()
