"""
NO_LOOKAHEAD_TEST_SUITE
Automated tests proving zero look-ahead bias across data, features, and strategy decisions.

Requirements:
- TEST A: Run backtest up to T. Modify data after T. All decisions before T must remain IDENTICAL.
- TEST B: Truncate data after T. Run strategy. Signals before T must be identical to full dataset.
- TEST C: Modify tomorrow's OHLC, volume, returns. Today's signals must not change.
- TEST D: Verify feature calculations (e.g. VWAP, ORB) only depend on past and present bars.
"""

from datetime import datetime, timedelta, time
import numpy as np
import pandas as pd
import pytest

from backtest.event_engine import EventDrivenBacktester, ExecutionPolicy
from config.settings import AppSettings, InstrumentConfig, RiskConfig, StrategyConfig
from indicators.orb import ORBCalculator
from indicators.vwap import calculate_session_vwap


def _generate_synthetic_candles(start_dt: datetime, num_days: int = 3) -> pd.DataFrame:
    """Generates standard intraday 15m candles across multiple days for deterministic testing."""
    records = []
    base_price = 24000.0

    current_dt = start_dt
    for day in range(num_days):
        # 09:15 to 15:15 (25 bars per day)
        day_date = current_dt.date()
        start_of_day = datetime.combine(day_date, time(9, 15))
        for b in range(25):
            bar_dt = start_of_day + timedelta(minutes=15 * b)

            # Mild predictable wave
            delta = np.sin(b / 3.0) * 30.0
            open_p = base_price + delta
            high_p = open_p + 15.0
            low_p = open_p - 15.0
            close_p = open_p + 5.0
            volume = 1000 + b * 50

            records.append({
                "datetime": bar_dt,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": volume,
            })
        current_dt += timedelta(days=1)

    df = pd.DataFrame(records)
    df["vwap"] = calculate_session_vwap(df).values
    return df


@pytest.fixture
def backtest_env():
    instrument = InstrumentConfig(
        symbol="NIFTY24SEPFUT",
        exchange="NFO",
        instrument_type="FUTURES",
        lot_size=25,
        min_orb_range=20.0,
        max_orb_range=150.0,
    )
    settings = AppSettings(
        risk=RiskConfig(initial_capital=500_000.0, risk_per_trade_pct=1.0, max_trades_per_day=2),
        strategy=StrategyConfig(),
    )
    engine = EventDrivenBacktester(
        instrument=instrument,
        app_settings=settings,
        execution_policy=ExecutionPolicy.CONSERVATIVE,
    )
    return engine, instrument, settings


def test_a_modify_future_data_decisions_before_t_identical(backtest_env):
    """
    TEST A:
    Record all decisions up to timestamp T.
    Modify all data AFTER T.
    Re-run backtest.
    All decisions BEFORE T must remain IDENTICAL.
    """
    engine, _, _ = backtest_env
    df_orig = _generate_synthetic_candles(datetime(2024, 9, 2, 9, 15), num_days=2)

    # Run original
    trades_orig = engine.generate_trades(df_orig.copy())

    # Pick cutoff timestamp T: end of Day 1 (15:15)
    t_cutoff = datetime(2024, 9, 2, 15, 15)

    # Decisions before T
    decisions_before_t_orig = [t for t in trades_orig if t["entry_time"] <= t_cutoff]

    # Create modified dataset: corrupt all data AFTER T (e.g. 5x price spike, huge volume)
    df_modified = df_orig.copy()
    future_mask = df_modified["datetime"] > t_cutoff
    df_modified.loc[future_mask, "open"] *= 2.5
    df_modified.loc[future_mask, "high"] *= 2.5
    df_modified.loc[future_mask, "low"] *= 2.5
    df_modified.loc[future_mask, "close"] *= 2.5
    df_modified.loc[future_mask, "volume"] *= 10
    df_modified["vwap"] = calculate_session_vwap(df_modified).values

    # Re-run
    trades_modified = engine.generate_trades(df_modified)
    decisions_before_t_mod = [t for t in trades_modified if t["entry_time"] <= t_cutoff]

    # Must be completely identical before T
    assert len(decisions_before_t_orig) == len(decisions_before_t_mod)
    for t1, t2 in zip(decisions_before_t_orig, decisions_before_t_mod):
        assert t1["entry_time"] == t2["entry_time"]
        assert t1["direction"] == t2["direction"]
        assert t1["entry_price"] == t2["entry_price"]
        assert t1["quantity"] == t2["quantity"]
        assert t1["initial_stop"] == t2["initial_stop"]
        assert t1["initial_target"] == t2["initial_target"]


def test_b_truncate_future_data_signals_before_t_identical(backtest_env):
    """
    TEST B:
    Truncate data after timestamp T.
    Signals before T must be identical between the truncated run and the full run.
    """
    engine, _, _ = backtest_env
    df_full = _generate_synthetic_candles(datetime(2024, 9, 2, 9, 15), num_days=3)

    trades_full = engine.generate_trades(df_full.copy())

    # Cutoff at midday Day 2
    t_cutoff = datetime(2024, 9, 3, 12, 0)
    full_before_t = [t for t in trades_full if t["entry_time"] <= t_cutoff]

    # Truncate dataset
    df_truncated = df_full[df_full["datetime"] <= t_cutoff].copy()
    df_truncated["vwap"] = calculate_session_vwap(df_truncated).values

    trades_trunc = engine.generate_trades(df_truncated)

    assert len(trades_trunc) == len(full_before_t)
    for t1, t2 in zip(trades_trunc, full_before_t):
        assert t1["entry_time"] == t2["entry_time"]
        assert t1["direction"] == t2["direction"]
        assert t1["entry_price"] == t2["entry_price"]


def test_c_modify_tomorrow_data_today_signals_unchanged(backtest_env):
    """
    TEST C:
    Change tomorrow's OHLC, volume, VWAP and verify today's signals do not change.
    """
    engine, _, _ = backtest_env
    df = _generate_synthetic_candles(datetime(2024, 9, 2, 9, 15), num_days=2)

    trades_run1 = engine.generate_trades(df.copy())
    day1_trades_run1 = [t for t in trades_run1 if t["entry_time"].date() == datetime(2024, 9, 2).date()]

    # Completely scramble tomorrow (Day 2)
    df_corrupted_tomorrow = df.copy()
    tomorrow_mask = df_corrupted_tomorrow["datetime"].dt.date == datetime(2024, 9, 3).date()
    df_corrupted_tomorrow.loc[tomorrow_mask, "open"] = 10000.0
    df_corrupted_tomorrow.loc[tomorrow_mask, "high"] = 10500.0
    df_corrupted_tomorrow.loc[tomorrow_mask, "low"] = 9500.0
    df_corrupted_tomorrow.loc[tomorrow_mask, "close"] = 10200.0
    df_corrupted_tomorrow.loc[tomorrow_mask, "volume"] = 999999
    df_corrupted_tomorrow["vwap"] = calculate_session_vwap(df_corrupted_tomorrow).values

    trades_run2 = engine.generate_trades(df_corrupted_tomorrow)
    day1_trades_run2 = [t for t in trades_run2 if t["entry_time"].date() == datetime(2024, 9, 2).date()]

    assert len(day1_trades_run1) == len(day1_trades_run2)
    for t1, t2 in zip(day1_trades_run1, day1_trades_run2):
        assert t1["entry_time"] == t2["entry_time"]
        assert t1["entry_price"] == t2["entry_price"]
        assert t1["pnl_net"] == t2["pnl_net"]


def test_d_vwap_and_orb_do_not_use_future_bars():
    """
    TEST D:
    Feature calculation check: VWAP at bar k must only depend on bars 0..k.
    ORB calculation at 09:45 must only depend on bars 09:15 and 09:30.
    """
    df = _generate_synthetic_candles(datetime(2024, 9, 2, 9, 15), num_days=1)

    # 1. Check VWAP causal progression
    for k in range(3, len(df)):
        sub_df = df.iloc[:k+1].copy()
        vwap_sub = calculate_session_vwap(sub_df).iloc[-1]
        vwap_full = df["vwap"].iloc[k]
        assert pytest.approx(vwap_sub, rel=1e-5) == vwap_full

    # 2. Check ORB causality: changing bar at 10:15 must NOT affect opening range
    orb1 = ORBCalculator.calculate_opening_range(df.set_index("datetime"))

    df_mod = df.copy()
    # Modify 10:30 candle
    mask = df_mod["datetime"].dt.time == time(10, 30)
    df_mod.loc[mask, "high"] = 999999.0
    df_mod.loc[mask, "low"] = 1.0

    orb2 = ORBCalculator.calculate_opening_range(df_mod.set_index("datetime"))
    assert orb1.high == orb2.high
    assert orb1.low == orb2.low
    assert orb1.width == orb2.width
