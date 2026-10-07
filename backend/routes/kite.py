"""
Kite Connect authentication routes (signal-only system).

Owns the OAuth redirect flow (/kite/login -> /kite/callback), session
status and logout.

REMOVED (signal-only system; the frontend only uses GET /kite/login,
GET /kite/status, POST /kite/logout):
  - GET /api/profile, GET /api/margins: account/margin reads that do not
    belong in a signal-only system.
  - POST /api/logout: exact duplicate of POST /kite/logout.
  - POST /api/login-url, POST /api/login, GET /api/status: earlier dead routes.
"""

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from kiteconnect import KiteConnect

from backend.security import verify_shared_secret
from backend.broker.kite_adapter import (
    clear_session,
    get_active_kite_with_diagnostics,
    get_saved_session,
    save_session,
)
from backend.config.settings import settings

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
    # `alias="status"` binds to the literal ?status= Kite sends, without
    # shadowing fastapi's `status` module used below.
    auth_status: Optional[str] = Query(None, alias="status"),
):
    """
    Zerodha OAuth redirect callback. Exchanges request_token for access_token,
    persists the session, and redirects back to the frontend.
    The request_token is never logged.
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
            "Successfully authenticated Kite session for user %s",
            session_data.get("user_id", ""),
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
    except Exception as exc:
        # Session validated above, so profile is cosmetic — but never swallow silently.
        logger.warning("kite.profile() failed during /kite/status: %s", exc)

    # No invented defaults: if Kite did not return it, it is empty/unknown.
    return {
        "connected": True,
        "user_id": (profile.get("user_id") if profile else None) or session.get("user_id", ""),
        "user_name": (profile.get("user_name") if profile else None) or session.get("user_name", ""),
        "products": (profile.get("products") if profile else None) or [],
        "exchanges": (profile.get("exchanges") if profile else None) or [],
    }


@router.post("/kite/logout")
def kite_logout(x_shared_secret: Optional[str] = Header(None, alias="X-Shared-Secret")):
    """Stops active stream and clears local access token and session state."""
    verify_shared_secret(x_shared_secret)
    try:
        from backend.streaming.market_stream_manager import market_stream_manager
        market_stream_manager.stop_stream()
    except Exception as exc:
        logger.warning("Error stopping stream on logout: %s", exc)
    clear_session()
    return {
        "success": True,
        "message": "Logged out successfully",
    }


logout = kite_logout


