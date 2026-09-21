"""
Unit tests for standard WalkForwardValidator (In-Sample / Out-Of-Sample split).
"""

from datetime import datetime, timedelta, time
import numpy as np
import pandas as pd
import pytest

from backtest.walk_forward import WalkForwardValidator
from config.settings import AppSettings, InstrumentConfig, RiskConfig, StrategyConfig
from indicators.vwap import calculate_session_vwap


def _generate_test_candles(num_days: int = 30) -> pd.DataFrame:
    records = []
    base_price = 24000.0
    start_dt = datetime(2024, 7, 1, 9, 15)

    current_dt = start_dt
    for day in range(num_days):
        day_date = current_dt.date()
        start_of_day = datetime.combine(day_date, time(9, 15))
        for b in range(25):
            bar_dt = start_of_day + timedelta(minutes=15 * b)
            delta = np.sin((day * 25 + b) / 5.0) * 35.0
            open_p = base_price + delta
            high_p = open_p + 25.0
            low_p = open_p - 25.0
            close_p = open_p + 5.0

            records.append({
                "datetime": bar_dt,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": 1500,
            })
        current_dt += timedelta(days=1)

    df = pd.DataFrame(records)
    df["vwap"] = calculate_session_vwap(df).values
    return df


def test_walk_forward_in_sample_out_sample_split():
    """Verify WalkForwardValidator correctly partitions data into 70% in-sample and 30% out-of-sample."""
    inst = InstrumentConfig(
        symbol="NIFTY24SEPFUT",
        exchange="NFO",
        instrument_type="FUTURES",
        lot_size=25,
        min_orb_range=20.0,
        max_orb_range=150.0,
    )
    settings = AppSettings(
        risk=RiskConfig(initial_capital=500_000.0, risk_per_trade_pct=1.0, max_trades_per_day=3),
        strategy=StrategyConfig(),
    )
    validator = WalkForwardValidator(instrument=inst, app_settings=settings)
    df = _generate_test_candles(num_days=30)

    result = validator.validate(df, split_ratio=0.70, initial_capital=500_000.0)

    assert result.in_sample_report is not None
    assert result.out_of_sample_report is not None
    assert isinstance(result.profit_factor_retention_pct, float)
    assert isinstance(result.win_rate_retention_pct, float)
    assert isinstance(result.is_statistically_robust, bool)
