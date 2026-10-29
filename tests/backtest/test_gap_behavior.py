"""
Unit tests for gap behavior: Gap through stop loss and gap through profit target.
"""

from datetime import datetime
import pandas as pd
import pytest

from backend.backtest.strategy_backtester import EventDrivenBacktester
from backend.config.settings import AppSettings, InstrumentConfig, InstrumentType


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
    # Exit-gap behaviour is tested with fill-at-signal-close entries; with the
    # default NEXT_BAR_OPEN policy the same gap cancels the entry instead
    # (see test_next_bar_open_cancels_entry_gapping_through_stop).
    engine = EventDrivenBacktester(instrument=inst, app_settings=settings, fill_policy="SIGNAL_CLOSE")
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
    # Exit-gap behaviour is tested with fill-at-signal-close entries; with the
    # default NEXT_BAR_OPEN policy the same gap cancels the entry instead
    # (see test_next_bar_open_cancels_entry_gapping_through_stop).
    engine = EventDrivenBacktester(instrument=inst, app_settings=settings, fill_policy="SIGNAL_CLOSE")
    trades = engine.generate_trades(df)

    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "PROFIT_TARGET"
    assert trades[0]["exit_price"] == 24085.0, f"Expected gap fill at 24085, got {trades[0]['exit_price']}"


def test_next_bar_open_cancels_entry_gapping_through_stop():
    """A breakout whose next open is already below the stop is never filled."""
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
        ("09:45", 24015, 24035, 24015, 24030),  # signal at close 24030, SL=24010
        ("10:00", 23990, 23995, 23980, 23985),  # next open already below SL
    ]
    engine = EventDrivenBacktester(instrument=inst, app_settings=settings)
    trades = engine.generate_trades(_make_day_candles(bars))
    assert trades == []
    assert engine.skipped_entries and "stop" in engine.skipped_entries[0]["reason"]


def test_next_bar_open_fills_at_real_open():
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
        ("09:45", 24015, 24035, 24015, 24030),  # signal at close 24030
        ("10:00", 24032, 24040, 24028, 24036),  # filled at this open
        ("10:15", 24036, 24038, 24030, 24034),
    ]
    engine = EventDrivenBacktester(instrument=inst, app_settings=settings)
    trades = engine.generate_trades(_make_day_candles(bars))
    assert len(trades) == 1
    assert trades[0]["entry_price"] == 24032.0
    assert trades[0]["signal_price"] == 24030.0
