"""
Unit Tests for HistoricalDataLoader and Real Data Enforcement.
"""

from datetime import datetime
from pathlib import Path
import pandas as pd
import pytest

from config.settings import InstrumentConfig, InstrumentType
from data.historical_loader import HistoricalDataLoader


def test_real_data_required_fails_when_unavailable(tmp_path, monkeypatch):
    """
    REAL_DATA_REQUIRED_TEST:
    If real data client is invalid/unavailable, fetch_real_data raises RuntimeError.
    """
    with pytest.raises(RuntimeError):
        HistoricalDataLoader.fetch_real_data(
            kite_client=None,
            instrument_token=99999999,
            start_date=datetime(2025, 1, 1).date(),
            end_date=datetime(2025, 1, 31).date(),
        )


def test_metadata_persistence(tmp_path):
    csv_file = tmp_path / "TEST_15m_test.csv"
    base = datetime(2025, 1, 1, 9, 15)
    df = pd_df = HistoricalDataLoader.generate_synthetic_nifty_data(days=2, base_price=24000.0)

    metadata = {
        "symbol": "TEST",
        "instrument_token": 12345,
        "interval": "15minute",
        "start_date": "2025-01-01",
        "end_date": "2025-01-02",
        "source": "Zerodha Kite Connect Historical API",
        "downloaded_timestamp": datetime.now().isoformat(),
        "candle_count": len(df),
        "trading_days": 2,
    }

    HistoricalDataLoader.save_with_metadata(df, csv_file, metadata)
    loaded_df, loaded_meta = HistoricalDataLoader.load_cached_data_with_validation(csv_file)

    assert len(loaded_df) == len(df)
    assert loaded_meta["symbol"] == "TEST"
    assert loaded_meta["instrument_token"] == 12345
    assert loaded_meta["candle_count"] == len(df)


def test_intraday_cache_refreshes_when_cache_is_behind(
    tmp_path,
    monkeypatch,
):
    cache_file = tmp_path / "TEST_15m.csv"

    cached = pd.DataFrame([
        {
            "datetime": datetime(2026, 10, 1, 9, 15),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000,
        },
        {
            "datetime": datetime(2026, 10, 1, 9, 30),
            "open": 100.5,
            "high": 102.0,
            "low": 100.0,
            "close": 101.5,
            "volume": 1100,
        },
    ])

    HistoricalDataLoader.save_with_metadata(
        cached,
        cache_file,
        {
            "symbol": "TEST",
            "instrument_token": 123,
            "interval": "15minute",
        },
    )

    fresh = pd.DataFrame([
        {
            "datetime": datetime(2026, 10, 1, 9, 30),
            "open": 100.5,
            "high": 102.0,
            "low": 100.0,
            "close": 101.5,
            "volume": 1100,
        },
        {
            "datetime": datetime(2026, 10, 1, 9, 45),
            "open": 101.5,
            "high": 103.0,
            "low": 101.0,
            "close": 102.5,
            "volume": 1200,
        },
        {
            "datetime": datetime(2026, 10, 1, 10, 0),
            "open": 102.5,
            "high": 104.0,
            "low": 102.0,
            "close": 103.5,
            "volume": 1300,
        },
    ])

    calls = {"count": 0}

    def fake_fetch_real_data(**kwargs):
        calls["count"] += 1
        return fresh.copy()

    monkeypatch.setattr(
        HistoricalDataLoader,
        "fetch_real_data",
        staticmethod(fake_fetch_real_data),
    )

    result = HistoricalDataLoader.load_or_refresh_intraday_cache(
        kite_client=object(),
        instrument_token=123,
        cache_path=cache_file,
        now=datetime(2026, 10, 1, 10, 7),
    )

    assert calls["count"] == 1
    assert result["datetime"].max() == datetime(
        2026, 10, 1, 9, 45
    )


def test_intraday_cache_does_not_refetch_when_current(
    tmp_path,
    monkeypatch,
):
    cache_file = tmp_path / "TEST_15m.csv"

    cached = pd.DataFrame([
        {
            "datetime": datetime(2026, 10, 1, 9, 15),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000,
        },
        {
            "datetime": datetime(2026, 10, 1, 9, 30),
            "open": 100.5,
            "high": 102.0,
            "low": 100.0,
            "close": 101.5,
            "volume": 1100,
        },
        {
            "datetime": datetime(2026, 10, 1, 9, 45),
            "open": 101.5,
            "high": 103.0,
            "low": 101.0,
            "close": 102.5,
            "volume": 1200,
        },
    ])

    HistoricalDataLoader.save_with_metadata(
        cached,
        cache_file,
        {
            "symbol": "TEST",
            "instrument_token": 123,
            "interval": "15minute",
        },
    )

    def should_not_be_called(**kwargs):
        raise AssertionError(
            "Historical API should not be called when cache is current."
        )

    monkeypatch.setattr(
        HistoricalDataLoader,
        "fetch_real_data",
        staticmethod(should_not_be_called),
    )

    result = HistoricalDataLoader.load_or_refresh_intraday_cache(
        kite_client=object(),
        instrument_token=123,
        cache_path=cache_file,
        now=datetime(2026, 10, 1, 10, 7),
    )

    assert result["datetime"].max() == datetime(
        2026, 10, 1, 9, 45
    )


def test_latest_completed_15m_candle():

    assert (
        HistoricalDataLoader.get_latest_completed_candle_start(
            datetime(2026, 10, 1, 10, 7)
        )
        == datetime(2026, 10, 1, 9, 45)
    )

    assert (
        HistoricalDataLoader.get_latest_completed_candle_start(
            datetime(2026, 10, 1, 10, 15)
        )
        == datetime(2026, 10, 1, 10, 0)
    )

    assert (
        HistoricalDataLoader.get_latest_completed_candle_start(
            datetime(2026, 10, 1, 9, 20)
        )
        is None
    )

