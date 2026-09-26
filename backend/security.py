"""
Shared-secret verification dependency used by sensitive backend routes
(placing/exiting orders, logging out). Split out of main.py so every
route module can depend on it without importing from main.py (which
would create a circular import once main.py includes their routers).
"""

import hmac
from typing import Optional

from fastapi import Header, HTTPException, status

from config.settings import settings


def verify_shared_secret(x_shared_secret: Optional[str] = Header(None, alias="X-Shared-Secret")):
    """Validates presence and correctness of shared secret token for sensitive actions."""
    expected_secret = settings.app_shared_secret
    if not expected_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server misconfigured: APP_SHARED_SECRET not set in .env",
        )
    if not x_shared_secret or not hmac.compare_digest(x_shared_secret, expected_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Missing or invalid X-Shared-Secret header.",
        )
    return True
