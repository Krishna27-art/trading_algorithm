"""
Integration tests for EventDrivenBacktester execution engine.
"""

from datetime import datetime, timedelta, time
import pandas as pd
import pytest

from backtest.event_engine import EventDrivenBacktester, ExecutionPolicy
from config.settings import AppSettings, InstrumentConfig, RiskConfig, StrategyConfig
from indicators.vwap import calculate_session_vwap


def _create_simple_session_data() -> pd.DataFrame:
    records = []
    d = datetime(2024, 9, 2).date()
    start_dt = datetime.combine(d, time(9, 15))

    # 25 bars: 09:15 to 15:15
    for b in range(25):
        bar_dt = start_dt + timedelta(minutes=15 * b)
        # ORB bars (b=0, 1: 09:15 and 09:30): high=24100, low=24000
        if b == 0:
            open_p, high_p, low_p, close_p = 24020.0, 24080.0, 24000.0, 24050.0
        elif b == 1:
            open_p, high_p, low_p, close_p = 24050.0, 24100.0, 24010.0, 24060.0
        # Breakout bar (b=2: 09:45): close=24120 > 24100
        elif b == 2:
            open_p, high_p, low_p, close_p = 24060.0, 24125.0, 24055.0, 24120.0
        # Target reached bar (b=3: 10:00): high=24400.0 >= 24360
        elif b == 3:
            open_p, high_p, low_p, close_p = 24120.0, 24400.0, 24110.0, 24380.0
        else:
            open_p, high_p, low_p, close_p = 24280.0, 24300.0, 24250.0, 24260.0

        records.append({
            "datetime": bar_dt,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": 2000,
        })

    df = pd.DataFrame(records)
    df["vwap"] = calculate_session_vwap(df).values
    return df


def test_event_engine_breakout_and_target_hit():
    """Verifies backtester captures breakout at 09:45 and exits at target."""
    inst = InstrumentConfig(
        symbol="NIFTY24SEPFUT",
        exchange="NFO",
        instrument_type="FUTURES",
        lot_size=25,
        min_orb_range=40.0,
        max_orb_range=120.0,
    )
    settings = AppSettings(
        risk=RiskConfig(initial_capital=500_000.0, risk_per_trade_pct=1.0, max_trades_per_day=3),
        strategy=StrategyConfig(),
    )
    engine = EventDrivenBacktester(instrument=inst, app_settings=settings)
    df = _create_simple_session_data()

    trades = engine.generate_trades(df)
    assert len(trades) == 1
    t = trades[0]
    assert t["direction"] == "BUY"
    assert t["entry_price"] == 24120.0
    assert t["exit_reason"] == "PROFIT_TARGET"
    assert t["pnl_net"] > 0
    assert t["total_costs"] > 0


def test_no_overnight_positions():
    """Ensure any open position at end of session is squared off before day end."""
    records = []
    d = datetime(2024, 9, 2).date()
    start_dt = datetime.combine(d, time(9, 15))

    # Candle where breakout happens late, no target/stop hit before 14:30
    for b in range(25):
        bar_dt = start_dt + timedelta(minutes=15 * b)
        if b < 2:
            open_p, high_p, low_p, close_p = 24000.0, 24050.0, 23990.0, 24020.0
        elif b == 2:  # Breakout
            open_p, high_p, low_p, close_p = 24020.0, 24080.0, 24010.0, 24070.0
        else:  # Stagnant flat price
            open_p, high_p, low_p, close_p = 24070.0, 24075.0, 24065.0, 24070.0

        records.append({
            "datetime": bar_dt,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": 1000,
        })

    df = pd.DataFrame(records)
    df["vwap"] = calculate_session_vwap(df).values

    inst = InstrumentConfig(
        symbol="NIFTY24SEPFUT",
        exchange="NFO",
        instrument_type="FUTURES",
        lot_size=25,
        min_orb_range=40.0,
        max_orb_range=120.0,
    )
    settings = AppSettings(
        risk=RiskConfig(initial_capital=500_000.0, risk_per_trade_pct=1.0, max_trades_per_day=3),
        strategy=StrategyConfig(),
    )
    engine = EventDrivenBacktester(instrument=inst, app_settings=settings)
    trades = engine.generate_trades(df)

    assert len(trades) == 1
    # Squared off by 14:30 time rule
    assert trades[0]["exit_reason"] in ("TIME_SQUARE_OFF", "SESSION_CLOSE")
    assert trades[0]["exit_time"].time() >= time(14, 30)
