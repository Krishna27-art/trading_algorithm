"""
Unit Tests for Technical Indicators: ORB, Session VWAP, and ATR.
"""

from datetime import datetime, time, timedelta
import numpy as np
import pandas as pd
import pytest

from indicators.atr import calculate_atr
from indicators.orb import ORBCalculator
from indicators.vwap import calculate_session_vwap


def test_orb_calculator_valid_volatility():
    # Construct 15-minute bars for 09:15 and 09:30
    dt1 = datetime(2026, 3, 2, 9, 15)
    dt2 = datetime(2026, 3, 2, 9, 30)

    data = [
        {"datetime": dt1, "open": 24000.0, "high": 24080.0, "low": 23990.0, "close": 24050.0, "volume": 1000},
        {"datetime": dt2, "open": 24050.0, "high": 24100.0, "low": 24020.0, "close": 24090.0, "volume": 1200},
    ]
    df = pd.DataFrame(data).set_index("datetime")

    orb = ORBCalculator.calculate_opening_range(df, min_orb_range=40.0)
    assert orb is not None
    assert orb.high == 24100.0
    assert orb.low == 23990.0
    assert orb.width == 110.0
    assert orb.is_valid_volatility is True


def test_orb_calculator_low_volatility_filter():
    # Range is only 20 points (< 40 threshold)
    dt1 = datetime(2026, 3, 2, 9, 15)
    dt2 = datetime(2026, 3, 2, 9, 30)

    data = [
        {"datetime": dt1, "open": 24000.0, "high": 24015.0, "low": 24000.0, "close": 24010.0, "volume": 1000},
        {"datetime": dt2, "open": 24010.0, "high": 24020.0, "low": 24005.0, "close": 24015.0, "volume": 1200},
    ]
    df = pd.DataFrame(data).set_index("datetime")

    orb = ORBCalculator.calculate_opening_range(df, min_orb_range=40.0)
    assert orb is not None
    assert orb.width == 20.0
    assert orb.is_valid_volatility is False


def test_session_vwap_calculation():
    d1 = datetime(2026, 3, 2, 9, 15)
    d2 = datetime(2026, 3, 2, 9, 30)
    data = [
        {"datetime": d1, "high": 102.0, "low": 98.0, "close": 100.0, "volume": 100}, # typ = 100, pv = 10000
        {"datetime": d2, "high": 106.0, "low": 102.0, "close": 104.0, "volume": 200}, # typ = 104, pv = 20800
    ]
    df = pd.DataFrame(data)
    vwap = calculate_session_vwap(df)

    # First bar VWAP = 100.0
    # Second bar VWAP = (10000 + 20800) / 300 = 30800 / 300 = 102.666...
    assert round(vwap.iloc[0], 2) == 100.0
    assert round(vwap.iloc[1], 2) == 102.67


def test_atr_calculation():
    dates = [datetime(2026, 1, 1) + timedelta(days=i) for i in range(20)]
    df = pd.DataFrame({
        "high": [100 + i for i in range(20)],
        "low": [90 + i for i in range(20)],
        "close": [95 + i for i in range(20)],
    }, index=dates)

    atr = calculate_atr(df, period=14)
    # Range is constant 10
    assert round(atr.dropna().iloc[-1], 2) == 10.0
