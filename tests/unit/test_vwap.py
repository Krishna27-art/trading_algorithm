"""
Unit tests for Intraday Session-Anchored VWAP calculation.
"""

from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import pytest

from indicators.vwap import calculate_session_vwap


def test_single_day_session_vwap():
    """Computes session VWAP with known hand-calculated values."""
    d1 = datetime(2024, 9, 2, 9, 15)
    d2 = datetime(2024, 9, 2, 9, 30)
    data = [
        {"datetime": d1, "high": 102.0, "low": 98.0, "close": 100.0, "volume": 100},  # typ=100, pv=10000
        {"datetime": d2, "high": 106.0, "low": 102.0, "close": 104.0, "volume": 200},  # typ=104, pv=20800
    ]
    df = pd.DataFrame(data)
    vwap = calculate_session_vwap(df)

    assert round(vwap.iloc[0], 2) == 100.0
    # (10000 + 20800) / (100 + 200) = 30800 / 300 = 102.666...
    assert round(vwap.iloc[1], 2) == 102.67


def test_multi_day_session_vwap_resets_at_open():
    """Session VWAP must reset cleanly at each day's 09:15 open and not bleed across days."""
    day1_bar1 = {"datetime": datetime(2024, 9, 2, 9, 15), "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000}
    day1_bar2 = {"datetime": datetime(2024, 9, 2, 15, 15), "high": 120.0, "low": 120.0, "close": 120.0, "volume": 1000}

    # Day 2 opens at 200.0
    day2_bar1 = {"datetime": datetime(2024, 9, 3, 9, 15), "high": 200.0, "low": 200.0, "close": 200.0, "volume": 500}

    df = pd.DataFrame([day1_bar1, day1_bar2, day2_bar1])
    vwap = calculate_session_vwap(df)

    # Day 1 end VWAP = (100*1000 + 120*1000) / 2000 = 110.0
    assert pytest.approx(vwap.iloc[1], rel=1e-4) == 110.0

    # Day 2 bar 1 VWAP must reset to 200.0, NOT blended with Day 1
    assert pytest.approx(vwap.iloc[2], rel=1e-4) == 200.0
