"""
Unit and integration tests for GET /api/market/prices (300-stock live market endpoint).
"""

from unittest.mock import MagicMock, patch
import pytest

from backend.market import get_market_prices


def test_market_prices_unauthenticated():
    """When Kite is disconnected, return AUTH_REQUIRED and empty stocks array."""
    with patch("backend.market.get_active_kite_with_diagnostics", return_value=(None, "Not logged in")):
        res = get_market_prices()
        assert res["status"] == "AUTH_REQUIRED"
        assert res["data_source"] == "NONE"
        assert res["count"] == 0
        assert res["stocks"] == []


def test_market_prices_authenticated_300_stocks():
    """When Kite is connected, return all 300 stocks with calculated change and percentage."""
    mock_kite = MagicMock()
    
    # Mock quote response for sample instruments
    def mock_quote(instruments):
        res = {}
        for inst in instruments:
            sym = inst.replace("NSE:", "")
            res[inst] = {
                "instrument_token": 12345,
                "last_price": 1000.0,
                "ohlc": {
                    "open": 990.0,
                    "high": 1010.0,
                    "low": 985.0,
                    "close": 980.0,  # prev_close
                },
                "volume": 500000,
                "average_price": 995.0,
            }
        return res

    mock_kite.quote.side_effect = mock_quote

    with patch("backend.market.get_active_kite_with_diagnostics", return_value=(mock_kite, None)):
        res = get_market_prices()
        assert res["status"] == "success"
        assert res["data_source"] == "REAL_KITE"
        assert res["count"] == 300
        assert len(res["stocks"]) == 300

        # Verify quote batching was called in batches of 150
        assert mock_kite.quote.call_count == 2
        batch1 = mock_kite.quote.call_args_list[0][0][0]
        batch2 = mock_kite.quote.call_args_list[1][0][0]
        assert len(batch1) == 150
        assert len(batch2) == 150

        # Verify stock calculations
        sample = res["stocks"][0]
        assert sample["ltp"] == 1000.0
        assert sample["prev_close"] == 980.0
        assert sample["open_price"] == 990.0
        assert sample["change"] == round(1000.0 - 980.0, 2)  # 20.0
        assert sample["change_pct"] == round(((1000.0 - 980.0) / 980.0) * 100, 2)  # 2.04%
        assert sample["volume"] == 500000
        assert sample["vwap"] == 995.0
        assert sample["status"] == "LIVE"


def test_market_prices_partial_data_unavailable():
    """When some quotes fail or are omitted, those rows are marked DATA_UNAVAILABLE while 300 rows are still returned."""
    mock_kite = MagicMock()
    # Return quotes only for RELIANCE
    mock_kite.quote.return_value = {
        "NSE:RELIANCE": {
            "instrument_token": 738561,
            "last_price": 1420.0,
            "ohlc": {"open": 1400.0, "close": 1400.0},
            "volume": 1000000,
            "average_price": 1410.0,
        }
    }

    with patch("backend.market.get_active_kite_with_diagnostics", return_value=(mock_kite, None)):
        res = get_market_prices()
        assert res["status"] == "success"
        assert res["count"] == 300
        assert len(res["stocks"]) == 300

        reliance = next(s for s in res["stocks"] if s["symbol"] == "RELIANCE")
        assert reliance["status"] == "LIVE"
        assert reliance["ltp"] == 1420.0

        other = next(s for s in res["stocks"] if s["symbol"] != "RELIANCE")
        assert other["status"] == "DATA_UNAVAILABLE"
        assert other["ltp"] is None
