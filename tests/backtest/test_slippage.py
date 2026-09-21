"""
Unit tests for configurable slippage modeling.
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


def test_slippage_friction():
    """
    TEST_SLIPPAGE:
    Verifies that statutory and configured execution slippage is correctly modeled
    in transaction costs and net P&L.
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
        ("09:45", 24015, 24035, 24015, 24030),  # Long entry at 24030, SL=24010, Target=24070
        ("10:00", 24030, 24075, 24025, 24070),  # Target hit at 24070
    ]
    df = _make_day_candles(bars)

    engine = EventDrivenBacktester(instrument=inst, app_settings=settings)
    trades = engine.generate_trades(df)

    assert len(trades) == 1
    t = trades[0]
    assert t["exit_reason"] == "PROFIT_TARGET"
    assert t["slippage_cost"] > 0
    assert t["total_costs"] > t["slippage_cost"]
    # Net P&L must be less than gross P&L by exactly total_costs
    assert round(t["pnl_gross"] - t["total_costs"], 2) == round(t["pnl_net"], 2)
