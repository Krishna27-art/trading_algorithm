"""
Unit tests for same-candle SL / Target collision resolution under different ExecutionPolicies.
"""

from datetime import datetime
import pandas as pd
import pytest

from backtest.event_engine import EventDrivenBacktester, ExecutionPolicy
from config.settings import AppSettings, InstrumentConfig, InstrumentType


def _make_day_candles(bars_data):
    """bars_data is list of (time_str, open, high, low, close)"""
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


def test_same_candle_conservative_policy_picks_stop_loss():
    """
    When a candle hits BOTH Target and Stop Loss:
    ExecutionPolicy.CONSERVATIVE must assume Stop Loss was hit first.
    """
    settings = AppSettings()
    inst = InstrumentConfig(
        symbol="NIFTY",
        exchange="NFO",
        instrument_type=InstrumentType.FUTURES,
        lot_size=25,
        min_orb_range=10.0,
        max_orb_range=120.0,
        max_risk_cap=80.0,
    )

    # 09:15 and 09:30 define OR: High=24020, Low=24010 (width=10)
    # 09:45: Breakout Long -> Close=24030 > OR High (24020). Entry=24030, SL=24010 (risk=20), Target=24070 (2R=40)
    # 10:00: Huge candle that hits BOTH Target (High=24080 >= 24070) AND Stop (Low=24005 <= 24010)
    bars = [
        ("09:15", 24010, 24020, 24010, 24015),
        ("09:30", 24015, 24020, 24010, 24015),
        ("09:45", 24015, 24035, 24015, 24030),  # Long entry at 24030, SL=24010, Target=24070
        ("10:00", 24030, 24080, 24005, 24050),  # Both hit! High 24080 >= 24070, Low 24005 <= 24010
        ("10:15", 24050, 24055, 24045, 24050),
    ]
    df = _make_day_candles(bars)

    # 1. Conservative (Default)
    engine_cons = EventDrivenBacktester(
        instrument=inst,
        app_settings=settings,
        execution_policy=ExecutionPolicy.CONSERVATIVE,
    )
    trades_cons = engine_cons.generate_trades(df)
    assert len(trades_cons) == 1
    assert trades_cons[0]["exit_reason"] in ("STOP_LOSS", "BREAKEVEN_SL")
    assert trades_cons[0]["pnl_net"] < 0

    # 2. Optimistic
    engine_opt = EventDrivenBacktester(
        instrument=inst,
        app_settings=settings,
        execution_policy=ExecutionPolicy.OPTIMISTIC,
    )
    trades_opt = engine_opt.generate_trades(df)
    assert len(trades_opt) == 1
    assert trades_opt[0]["exit_reason"] == "PROFIT_TARGET"
    assert trades_opt[0]["pnl_net"] > 0


def test_same_candle_sl_only():
    settings = AppSettings()
    inst = InstrumentConfig(symbol="NIFTY", exchange="NFO", instrument_type=InstrumentType.FUTURES, lot_size=25, min_orb_range=10.0)
    bars = [
        ("09:15", 24010, 24020, 24010, 24015),
        ("09:30", 24015, 24020, 24010, 24015),
        ("09:45", 24015, 24035, 24015, 24030),  # Entry=24030, SL=24010, Target=24070
        ("10:00", 24030, 24040, 24005, 24020),  # Hits SL only (Low 24005 <= 24010, High 24040 < 24070)
    ]
    df = _make_day_candles(bars)
    engine = EventDrivenBacktester(instrument=inst, app_settings=settings)
    trades = engine.generate_trades(df)
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "STOP_LOSS"


def test_same_candle_target_only():
    settings = AppSettings()
    inst = InstrumentConfig(symbol="NIFTY", exchange="NFO", instrument_type=InstrumentType.FUTURES, lot_size=25, min_orb_range=10.0)
    bars = [
        ("09:15", 24010, 24020, 24010, 24015),
        ("09:30", 24015, 24020, 24010, 24015),
        ("09:45", 24015, 24035, 24015, 24030),  # Entry=24030, SL=24010, Target=24070
        ("10:00", 24030, 24075, 24025, 24070),  # Hits Target only (High 24075 >= 24070, Low 24025 > 24010)
    ]
    df = _make_day_candles(bars)
    engine = EventDrivenBacktester(instrument=inst, app_settings=settings)
    trades = engine.generate_trades(df)
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "PROFIT_TARGET"


def test_breakeven_trailing_then_sl():
    settings = AppSettings()
    inst = InstrumentConfig(symbol="NIFTY", exchange="NFO", instrument_type=InstrumentType.FUTURES, lot_size=25, min_orb_range=10.0)
    # Entry=24030, SL=24010 (risk=20). +1R is 24050.
    bars = [
        ("09:15", 24010, 24020, 24010, 24015),
        ("09:30", 24015, 24020, 24010, 24015),
        ("09:45", 24015, 24035, 24015, 24030),  # Entry 24030
        ("10:00", 24030, 24055, 24025, 24052),  # High 24055 >= 24050 -> Breakeven trailing activated! SL moved to 24030
        ("10:15", 24052, 24055, 24028, 24035),  # Low 24028 <= 24030 -> Breakeven SL hit!
    ]
    df = _make_day_candles(bars)
    engine = EventDrivenBacktester(instrument=inst, app_settings=settings)
    trades = engine.generate_trades(df)
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "BREAKEVEN_SL"
    # Gross exit price should be 24030 (breakeven)
    assert trades[0]["exit_price"] == 24030.0
