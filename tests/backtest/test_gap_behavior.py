"""
Unit tests for gap behavior: Gap through stop loss and gap through profit target.
"""

from datetime import datetime
import pandas as pd
import pytest

from backtest.event_engine import EventDrivenBacktester
from config.settings import AppSettings, InstrumentConfig, InstrumentType


def _make_day_candles(bars_data):
    rows = []
    base_date = "2025-01-02"
    for t_str, o, h, l, c in bars_data:
        rows.append({
            "datetime": pd.to_datetime(f"{base_date} {t_str}"),
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": 10000,
        })
    return pd.DataFrame(rows)


def test_gap_through_stop():
    """
    TEST_GAP_THROUGH_STOP:
    Long Entry = 24030, SL = 24010.
    Next candle opens at 23990 (below the 24010 stop).
    The system MUST NOT report an exit at 24010 as though liquidity existed there.
    It MUST fill at Open (23990).
    """
    settings = AppSettings()
    inst = InstrumentConfig(
        symbol="NIFTY",
        exchange="NFO",
        instrument_type=InstrumentType.FUTURES,
        lot_size=25,
        min_orb_range=10.0,
    )

    bars = [
        ("09:15", 24010, 24020, 24010, 24015),
        ("09:30", 24015, 24020, 24010, 24015),
        ("09:45", 24015, 24035, 24015, 24030),  # Long entry at 24030, SL=24010
        ("10:00", 23990, 23995, 23980, 23985),  # GAPS DOWN: Open=23990 < SL=24010
    ]
    df = _make_day_candles(bars)
    engine = EventDrivenBacktester(instrument=inst, app_settings=settings)
    trades = engine.generate_trades(df)

    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "STOP_LOSS"
    # Exit price must reflect the gap open (23990), NOT the ideal 24010!
    assert trades[0]["exit_price"] == 23990.0, f"Expected gap fill at 23990, got {trades[0]['exit_price']}"


def test_gap_through_target():
    """
    TEST_GAP_THROUGH_TARGET:
    Long Entry = 24030, Target = 24070.
    Next candle opens at 24085 (above target 24070).
    Fill price must capture the favorable gap at 24085.
    """
    settings = AppSettings()
    inst = InstrumentConfig(
        symbol="NIFTY",
        exchange="NFO",
        instrument_type=InstrumentType.FUTURES,
        lot_size=25,
        min_orb_range=10.0,
    )

    bars = [
        ("09:15", 24010, 24020, 24010, 24015),
        ("09:30", 24015, 24020, 24010, 24015),
        ("09:45", 24015, 24035, 24015, 24030),  # Long entry at 24030, Target=24070
        ("10:00", 24085, 24095, 24080, 24090),  # GAPS UP: Open=24085 > Target=24070
    ]
    df = _make_day_candles(bars)
    engine = EventDrivenBacktester(instrument=inst, app_settings=settings)
    trades = engine.generate_trades(df)

    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "PROFIT_TARGET"
    assert trades[0]["exit_price"] == 24085.0, f"Expected gap fill at 24085, got {trades[0]['exit_price']}"
