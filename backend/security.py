"""
Shared-secret verification dependency used by sensitive backend routes
(stream start/stop, logout, research/backtest). Split out of main.py so every
route module can depend on it without importing from main.py (which would
create a circular import once main.py includes their routers).
"""

import hmac
from typing import Optional

from fastapi import Header, HTTPException, status

from backend.config.settings import settings


def verify_shared_secret(x_shared_secret: Optional[str] = Header(None, alias="X-Shared-Secret")):
    """Validates presence and correctness of shared secret token for sensitive actions."""
    expected_secret = settings.app_shared_secret
    if not expected_secret or expected_secret.strip() in {
        "CHANGE_ME_GENERATE_A_REAL_SECRET",
        "changeme",
        "secret",
        "your_secret_here",
    }:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server misconfigured: Insecure or placeholder APP_SHARED_SECRET.",
        )
    # hmac.compare_digest() raises TypeError on non-ASCII *str* input, which
    # turned a malformed header into an HTTP 500. Compare as UTF-8 bytes.
    provided = (x_shared_secret or "").encode("utf-8")
    if not provided or not hmac.compare_digest(provided, expected_secret.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Missing or invalid X-Shared-Secret header.",
        )
    return True
