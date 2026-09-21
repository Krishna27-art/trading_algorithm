"""
Unit tests for EODPanelLoader using mocked Kite API responses.
Verifies wide DataFrame alignment, NaN preservation for missing symbols, and disk caching.
"""

from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from data.eod_panel_loader import EODPanelLoader


def _create_mock_daily_candles(start_date: date, days: int, base_price: float = 1000.0) -> list:
    candles = []
    cur = start_date
    p = base_price
    for i in range(days):
        if cur.weekday() < 5:  # Monday to Friday
            candles.append({
                "date": datetime.combine(cur, datetime.min.time()).isoformat(),
                "open": round(p, 2),
                "high": round(p * 1.01, 2),
                "low": round(p * 0.99, 2),
                "close": round(p * 1.005, 2),
                "volume": 100000 + i * 500,
            })
            p = p * 1.002
        cur += timedelta(days=1)
    return candles


class MockKiteClient:
    def __init__(self):
        self.call_count = 0

    def historical_data(self, instrument_token: int, from_date: str, to_date: str, interval: str = "day", **kwargs):
        self.call_count += 1
        start_d = date(2025, 1, 1)
        if instrument_token == 256265:  # NIFTY
            return _create_mock_daily_candles(start_d, 20, 24000.0)
        elif instrument_token == 738561:  # RELIANCE
            return _create_mock_daily_candles(start_d, 20, 1400.0)
        elif instrument_token == 2953217:  # TCS
            # Returns partial data (first 8 sessions only)
            return _create_mock_daily_candles(start_d, 8, 3800.0)
        elif instrument_token == 999999:  # UNKNOWN / MISSING
            return []
        return []


@pytest.fixture
def mock_kite():
    return MockKiteClient()


def test_eod_panel_loader_returns_wide_frames(tmp_path, mock_kite):
    loader = EODPanelLoader(kite_client=mock_kite, cache_dir=tmp_path)

    # Mock token resolver
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "data.instrument_resolver.instrument_resolver.resolve_token",
            lambda symbol, exchange, kite_client: {
                "NIFTY": 256265,
                "RELIANCE": 738561,
                "TCS": 2953217,
            }.get(symbol),
        )

        closes, highs, lows, volumes, index_close = loader.load_panel(
            symbols=["RELIANCE", "TCS"],
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 20),
        )

        assert isinstance(closes, pd.DataFrame)
        assert isinstance(highs, pd.DataFrame)
        assert isinstance(lows, pd.DataFrame)
        assert isinstance(volumes, pd.DataFrame)
        assert isinstance(index_close, pd.Series)

        assert list(closes.columns) == ["RELIANCE", "TCS"]
        assert len(closes) > 0
        assert len(index_close) == len(closes)


def test_eod_panel_loader_preserves_nan_for_missing_and_partial_data(tmp_path, mock_kite):
    loader = EODPanelLoader(kite_client=mock_kite, cache_dir=tmp_path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "data.instrument_resolver.instrument_resolver.resolve_token",
            lambda symbol, exchange, kite_client: {
                "NIFTY": 256265,
                "RELIANCE": 738561,
                "TCS": 2953217,
                "MISSING": 999999,
            }.get(symbol),
        )

        closes, highs, lows, volumes, _ = loader.load_panel(
            symbols=["RELIANCE", "TCS", "MISSING"],
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 20),
        )

        # "MISSING" column must be present and all NaNs (not dropped!)
        assert "MISSING" in closes.columns
        assert closes["MISSING"].isna().all()

        # "TCS" has partial data: first entries are non-null, later sessions are NaN
        assert "TCS" in closes.columns
        assert closes["TCS"].iloc[0] > 0
        assert closes["TCS"].isna().any()  # Last sessions must be NaN, NOT forward-filled!


def test_eod_panel_loader_caches_locally(tmp_path, mock_kite):
    loader = EODPanelLoader(kite_client=mock_kite, cache_dir=tmp_path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "data.instrument_resolver.instrument_resolver.resolve_token",
            lambda symbol, exchange, kite_client: 738561 if symbol == "RELIANCE" else 256265,
        )

        loader.fetch_symbol_daily(
            symbol="RELIANCE",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 10),
            token=738561,
        )

        cache_file = tmp_path / "RELIANCE_day.csv"
        assert cache_file.exists()

        # Calling again should read from cache without invoking mock_kite.historical_data again for RELIANCE
        mock_kite.call_count = 0
        loader.fetch_symbol_daily(
            symbol="RELIANCE",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 10),
            token=738561,
            force_refresh=False,
        )
        assert mock_kite.call_count == 0
