"""
Unit Tests for Historical Data Validation (OHLCV integrity, timestamps, duplicates).
"""

from datetime import datetime, timedelta
import pandas as pd
import pytest

from data.historical_loader import HistoricalDataLoader


def test_valid_candles_pass_validation():
    base = datetime(2025, 1, 1, 9, 15)
    rows = []
    for i in range(10):
        t = base + timedelta(minutes=15 * i)
        rows.append({
            "datetime": t,
            "open": 24000.0 + i,
            "high": 24010.0 + i,
            "low": 23990.0 + i,
            "close": 24005.0 + i,
            "volume": 1000 + i * 10,
        })
    df = pd.DataFrame(rows)
    is_valid, errors = HistoricalDataLoader.validate_candles(df)
    assert is_valid is True
    assert len(errors) == 0


def test_validation_fails_on_invalid_high_low():
    base = datetime(2025, 1, 1, 9, 15)
    df = pd.DataFrame([
        {"datetime": base, "open": 24000.0, "high": 23900.0, "low": 24100.0, "close": 24050.0, "volume": 100}
    ])
    is_valid, errors = HistoricalDataLoader.validate_candles(df)
    assert is_valid is False
    assert any("High is lower" in e or "Low is higher" in e for e in errors)


def test_validation_fails_on_negative_prices_or_volume():
    base = datetime(2025, 1, 1, 9, 15)
    df = pd.DataFrame([
        {"datetime": base, "open": -100.0, "high": 24000.0, "low": -200.0, "close": 24000.0, "volume": -50}
    ])
    is_valid, errors = HistoricalDataLoader.validate_candles(df)
    assert is_valid is False
    assert any("Non-positive prices" in e for e in errors)
    assert any("Negative volume" in e for e in errors)


def test_validation_fails_on_duplicate_or_out_of_order_timestamps():
    t1 = datetime(2025, 1, 1, 9, 15)
    t2 = datetime(2025, 1, 1, 9, 30)

    # Out of order
    df_order = pd.DataFrame([
        {"datetime": t2, "open": 100.0, "high": 105.0, "low": 95.0, "close": 102.0, "volume": 10},
        {"datetime": t1, "open": 100.0, "high": 105.0, "low": 95.0, "close": 102.0, "volume": 10},
    ])
    is_valid, errors = HistoricalDataLoader.validate_candles(df_order)
    assert is_valid is False
    assert any("chronological order" in e for e in errors)

    # Duplicate
    df_dup = pd.DataFrame([
        {"datetime": t1, "open": 100.0, "high": 105.0, "low": 95.0, "close": 102.0, "volume": 10},
        {"datetime": t1, "open": 100.0, "high": 105.0, "low": 95.0, "close": 102.0, "volume": 10},
    ])
    is_valid, errors = HistoricalDataLoader.validate_candles(df_dup)
    assert is_valid is False
    assert any("duplicate timestamps" in e for e in errors)
