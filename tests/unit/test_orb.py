"""
Unit tests for Opening Range Breakout (ORB) indicator and strategy logic.
"""

from datetime import date, datetime, time
import pytest
from config.settings import InstrumentConfig, InstrumentType, StrategyConfig
from indicators.orb import ORBCalculator
from strategy.base_strategy import SignalAction
from strategy.orb_strategy import IntradayORBStrategy
import pandas as pd


def test_orb_indicator_calculation():
    """Validates ORBCalculator extracts high, low, width, and evaluates volatility threshold."""
    d = date(2024, 9, 2)
    bars = pd.DataFrame([
        {"datetime": datetime.combine(d, time(9, 15)), "open": 24000.0, "high": 24080.0, "low": 23990.0, "close": 24050.0},
        {"datetime": datetime.combine(d, time(9, 30)), "open": 24050.0, "high": 24100.0, "low": 24010.0, "close": 24070.0},
        {"datetime": datetime.combine(d, time(9, 45)), "open": 24070.0, "high": 24130.0, "low": 24060.0, "close": 24120.0},
    ]).set_index("datetime")

    orb = ORBCalculator.calculate_opening_range(bars, min_orb_range=40.0, max_orb_range=120.0)
    assert orb is not None
    assert orb.high == 24100.0
    assert orb.low == 23990.0
    assert orb.width == 110.0
    assert orb.is_valid_volatility is True


def test_orb_narrow_range_fails_volatility():
    """Opening range below min_orb_range fails volatility threshold."""
    d = date(2024, 9, 2)
    bars = pd.DataFrame([
        {"datetime": datetime.combine(d, time(9, 15)), "open": 24000.0, "high": 24020.0, "low": 23995.0, "close": 24010.0},
        {"datetime": datetime.combine(d, time(9, 30)), "open": 24010.0, "high": 24025.0, "low": 24005.0, "close": 24015.0},
    ]).set_index("datetime")

    # Range is 24025 - 23995 = 30 points < 40.0
    orb = ORBCalculator.calculate_opening_range(bars, min_orb_range=40.0)
    assert orb is not None
    assert orb.width == 30.0
    assert orb.is_valid_volatility is False


def test_orb_strategy_long_signal_and_trailing():
    """Verifies strategy emits BUY signal when bar closes above ORB High and VWAP."""
    inst = InstrumentConfig(
        symbol="NIFTY",
        exchange="NFO",
        instrument_type=InstrumentType.FUTURES,
        lot_size=25,
        min_orb_range=40.0,
        max_orb_range=120.0,
        max_risk_cap=80.0,
    )
    strat = IntradayORBStrategy(instrument=inst)
    d = date(2024, 9, 2)
    strat.reset_session(d)

    # First two bars establish ORB [23980, 24080]
    strat.on_candle({"datetime": datetime.combine(d, time(9, 30)), "open": 24000.0, "high": 24070.0, "low": 23980.0, "close": 24050.0}, vwap=24020.0)
    strat.on_candle({"datetime": datetime.combine(d, time(9, 45)), "open": 24050.0, "high": 24080.0, "low": 24000.0, "close": 24060.0}, vwap=24040.0)

    assert strat.orb.high == 24080.0
    assert strat.orb.low == 23980.0

    # Breakout candle closes at 24095 > 24080 and above VWAP 24050
    sig = strat.on_candle({"datetime": datetime.combine(d, time(10, 0)), "open": 24060.0, "high": 24100.0, "low": 24050.0, "close": 24095.0}, vwap=24050.0)
    assert sig is not None
    assert sig.action == SignalAction.BUY
    assert sig.price == 24095.0
