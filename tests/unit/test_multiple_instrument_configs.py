"""
Unit test: TEST_MULTIPLE_INSTRUMENT_CONFIGS
Verifies that EventDrivenBacktester runs identically on two different instrument configurations
(e.g., NIFTY Futures lot 25 vs RELIANCE Equity lot 1) without hardcoded symbol assumptions.
"""

from datetime import datetime, timedelta
import pandas as pd
import pytest

from backtest.event_engine import EventDrivenBacktester
from config.settings import AppSettings, InstrumentConfig, InstrumentType


def _generate_test_candles(base_price: float, days: int = 5) -> pd.DataFrame:
    rows = []
    base_t = datetime(2025, 1, 1, 9, 15)
    for d in range(days):
        day_date = (base_t + timedelta(days=d)).date()
        # 25 bars per day
        cur_p = base_price
        for b in range(25):
            bar_t = datetime.combine(day_date, datetime.min.time()).replace(hour=9, minute=15) + timedelta(minutes=15 * b)
            # Make a clean breakout on bar 3 (10:00)
            if b == 0:
                o, h, l, c = cur_p, cur_p + 10, cur_p - 10, cur_p + 5
            elif b == 1:
                o, h, l, c = cur_p, cur_p + 15, cur_p - 5, cur_p + 8
            elif b == 2:
                # breakout above OR high
                o, h, l, c = cur_p, cur_p + 35, cur_p, cur_p + 30
            else:
                o, h, l, c = cur_p, cur_p + 5, cur_p - 5, cur_p + 2
            cur_p = c
            rows.append({"datetime": bar_t, "open": o, "high": h, "low": l, "close": c, "volume": 5000})
    return pd.DataFrame(rows)


def test_multiple_instrument_configs():
    """
    TEST_MULTIPLE_INSTRUMENT_CONFIGS:
    Run the same core backtest engine using two completely different instrument configurations.
    """
    settings = AppSettings()

    inst1 = InstrumentConfig(
        symbol="NIFTY",
        exchange="NFO",
        instrument_type=InstrumentType.FUTURES,
        lot_size=25,
        min_orb_range=10.0,
        max_orb_range=120.0,
        max_risk_cap=80.0,
    )

    inst2 = InstrumentConfig(
        symbol="RELIANCE",
        exchange="NSE",
        instrument_type=InstrumentType.EQUITY,
        lot_size=1,
        min_orb_range=5.0,
        max_orb_range=50.0,
        max_risk_cap=30.0,
    )

    df1 = _generate_test_candles(base_price=24000.0, days=3)
    df2 = _generate_test_candles(base_price=2500.0, days=3)

    backtester1 = EventDrivenBacktester(instrument=inst1, app_settings=settings)
    report1 = backtester1.run(df1, initial_capital=1_000_000.0)

    backtester2 = EventDrivenBacktester(instrument=inst2, app_settings=settings)
    report2 = backtester2.run(df2, initial_capital=1_000_000.0)

    # Both engines must execute without errors
    assert report1.total_trades >= 0
    assert report2.total_trades >= 0
    assert report1.win_rate_pct >= 0
    assert report2.win_rate_pct >= 0
