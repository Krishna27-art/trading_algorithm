"""
Unit tests for backend CORS and API Authentication (X-Shared-Secret).
"""

import pytest
from fastapi import HTTPException
from backend.main import app
from backend.security import verify_shared_secret
from backend.routes.kite import logout
from backend.config.settings import settings


def test_cors_no_wildcard():
    # Verify that allow_origins does not contain '*'
    cors_middleware = [m for m in app.user_middleware if "CORSMiddleware" in str(m)]
    assert len(cors_middleware) > 0
    # Inspect kwargs
    kwargs = cors_middleware[0].kwargs
    allow_origins = kwargs.get("allow_origins", [])
    assert "*" not in allow_origins
    assert "http://localhost:5173" in allow_origins
    assert "http://127.0.0.1:5173" in allow_origins


def test_verify_shared_secret_function():
    # Missing secret -> 401
    with pytest.raises(HTTPException) as exc_missing:
        verify_shared_secret(x_shared_secret=None)
    assert exc_missing.value.status_code == 401
    assert "X-Shared-Secret" in exc_missing.value.detail

    # Invalid secret -> 401
    with pytest.raises(HTTPException) as exc_invalid:
        verify_shared_secret(x_shared_secret="wrong-secret")
    assert exc_invalid.value.status_code == 401

    # Correct secret -> True
    assert verify_shared_secret(x_shared_secret=settings.app_shared_secret) is True


def test_logout_endpoint_auth():
    # Without header -> 401
    with pytest.raises(HTTPException) as exc:
        logout(x_shared_secret=None)
    assert exc.value.status_code == 401

    # With correct header -> 200
    res = logout(x_shared_secret=settings.app_shared_secret)
    assert res["success"] is True


