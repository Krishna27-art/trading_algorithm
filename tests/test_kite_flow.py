"""
Unit Tests for Kite Authentication, Instrument Token Resolution, and Historical Data Flow.
"""

from datetime import date, datetime
from unittest.mock import MagicMock
import pytest
from fastapi import HTTPException

from data.instrument_resolver import InstrumentResolver, instrument_resolver
from data.historical_loader import HistoricalDataLoader
from backend.main import get_saved_session, get_active_kite_with_diagnostics, trigger_backtest


def test_instrument_resolver_canonical_indices():
    resolver = InstrumentResolver()
    # NIFTY 50 canonical token
    nifty_tok = resolver.resolve_token("NIFTY", exchange="NSE")
    assert nifty_tok == 256265

    # BANKNIFTY canonical token
    bn_tok = resolver.resolve_token("BANKNIFTY", exchange="NSE")
    assert bn_tok == 260105


def test_instrument_resolver_mock_instruments():
    resolver = InstrumentResolver()
    mock_kite = MagicMock()
    mock_kite.instruments.return_value = [
        {"instrument_token": 738561, "tradingsymbol": "RELIANCE", "exchange": "NSE", "lot_size": 1},
        {"instrument_token": 295321, "tradingsymbol": "TCS", "exchange": "NSE", "lot_size": 1},
    ]

    rel_tok = resolver.resolve_token("RELIANCE", exchange="NSE", kite_client=mock_kite)
    assert rel_tok == 738561

    tcs_tok = resolver.resolve_token("TCS", exchange="NSE", kite_client=mock_kite)
    assert tcs_tok == 295321


def test_historical_loader_supports_raw_kiteconnect():
    mock_kite = MagicMock()
    # Simulates kiteconnect.KiteConnect which has .historical_data() but NOT .get_historical_candles()
    del mock_kite.get_historical_candles
    mock_kite.historical_data.return_value = [
        {
            "date": "2025-01-02 09:15:00",
            "open": 24000.0,
            "high": 24050.0,
            "low": 23980.0,
            "close": 24020.0,
            "volume": 15000,
        },
        {
            "date": "2025-01-02 09:30:00",
            "open": 24020.0,
            "high": 24080.0,
            "low": 24010.0,
            "close": 24070.0,
            "volume": 12000,
        },
    ]

    df = HistoricalDataLoader.fetch_real_data(
        kite_client=mock_kite,
        instrument_token=256265,
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 2),
        interval="15minute",
        request_pause_seconds=0.0,
    )

    assert len(df) == 2
    assert "close" in df.columns
    assert df["close"].iloc[-1] == 24070.0
    mock_kite.historical_data.assert_called_once()


def test_historical_loader_propagates_exact_kite_error():
    mock_kite = MagicMock()
    del mock_kite.get_historical_candles
    mock_kite.historical_data.side_effect = PermissionError("Historical API subscription is required")

    with pytest.raises(RuntimeError) as exc:
        HistoricalDataLoader.fetch_real_data(
            kite_client=mock_kite,
            instrument_token=256265,
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 2),
            interval="15minute",
            request_pause_seconds=0.0,
        )

    assert "Historical API (PermissionError)" in str(exc.value)
    assert "Historical API subscription is required" in str(exc.value)


def test_backtest_endpoint_reports_diagnostics_without_kite(monkeypatch):
    # Simulate production mode where synthetic test data is disabled
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(HTTPException) as exc:
        trigger_backtest(days=5, symbol="NIFTY", strategy="cpr")
    assert exc.value.status_code == 400
    assert "Historical data unavailable" in exc.value.detail or "session" in exc.value.detail

