import logging
from unittest.mock import MagicMock
import pytest

from backend.kite import kite_callback


def test_kite_callback_never_logs_request_token(caplog):
    secret_token = "TEST_SECRET_REQUEST_TOKEN_999"
    with caplog.at_level(logging.ERROR):
        response = kite_callback(
            request_token=secret_token,
            auth_status="cancelled",
        )

    # Must redirect with failure
    assert response.status_code == 307
    assert "Kite+login+was+cancelled" in response.headers["location"]

    # Must NOT contain the secret request token anywhere in the log
    assert secret_token not in caplog.text


def test_kite_callback_exception_sanitizes_redirect_url(monkeypatch):
    from kiteconnect import KiteConnect

    secret_token = "TEST_SECRET_REQUEST_TOKEN_ABC"

    def mock_generate_session(*args, **kwargs):
        raise ValueError("Internal secret API key / network traceback details here")

    mock_kite_instance = MagicMock()
    mock_kite_instance.generate_session = mock_generate_session

    monkeypatch.setattr(
        "backend.kite.KiteConnect",
        lambda *args, **kwargs: mock_kite_instance,
    )
    monkeypatch.setattr(
        "backend.kite.settings.kite_api_key",
        "mock_api_key",
    )
    monkeypatch.setattr(
        "backend.kite.settings.kite_api_secret",
        "mock_api_secret",
    )

    response = kite_callback(
        request_token=secret_token,
        auth_status="success",
    )

    assert response.status_code == 307
    location = response.headers["location"]

    # Redirect URL must contain generic message, not raw exception details or token
    assert "auth_error=Authentication+failed." in location
    assert "Internal+secret" not in location
    assert secret_token not in location
