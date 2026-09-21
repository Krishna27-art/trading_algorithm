"""
Tests for Rolling Walk-Forward Robustness Validation.
Phase 11 Requirement:
- Chronological OOS blocks.
- No test window may use future data.
- Capital carries over from fold to fold.
- Evaluation of fold statistics: median PF, worst fold, best fold, dispersion, % profitable folds.
- OOS aggregate performance.
"""

from datetime import datetime, timedelta, time
import numpy as np
import pandas as pd
import pytest

from backtest.rolling_walk_forward import RollingWalkForwardValidator
from config.settings import AppSettings, InstrumentConfig, RiskConfig, StrategyConfig
from indicators.vwap import calculate_session_vwap


def _generate_multi_day_data(num_days: int = 40) -> pd.DataFrame:
    records = []
    base_price = 24000.0
    start_dt = datetime(2024, 6, 1, 9, 15)

    current_dt = start_dt
    for day in range(num_days):
        day_date = current_dt.date()
        start_of_day = datetime.combine(day_date, time(9, 15))
        for b in range(25):
            bar_dt = start_of_day + timedelta(minutes=15 * b)
            # Volatile price pattern with breakouts
            delta = np.sin((day * 25 + b) / 4.0) * 45.0
            open_p = base_price + delta
            high_p = open_p + 35.0
            low_p = open_p - 35.0
            close_p = open_p + 10.0
            volume = 2000 + b * 100

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
def wf_validator():
    instrument = InstrumentConfig(
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
    return RollingWalkForwardValidator(instrument=instrument, app_settings=settings)


def test_rolling_walk_forward_chronological_windows(wf_validator):
    """
    Verifies:
    1. Every fold's train window is strictly before its test window.
    2. Fold test windows are strictly chronological without overlap.
    3. Capital carries over continuously.
    4. FoldStatistics are properly calculated.
    """
    df = _generate_multi_day_data(num_days=35)

    # 20 days train, 5 days test per block -> 3 folds (days 20-25, 25-30, 30-35)
    result = wf_validator.validate(
        df_15m=df,
        min_train_days=20,
        test_block_days=5,
        initial_capital=500_000.0,
    )

    assert len(result.folds) == 3

    # Check chronological ordering and no look-ahead
    prev_test_end = None
    for i, fold in enumerate(result.folds):
        # Invariant: train_end strictly precedes test_start
        assert fold.train_end < fold.test_start

        # Invariant: consecutive test blocks are ordered chronologically
        if prev_test_end is not None:
            assert fold.test_start > prev_test_end
        prev_test_end = fold.test_end

        # Invariant: capital continuity
        if i > 0:
            assert result.folds[i].starting_capital == result.folds[i - 1].ending_capital

    # Invariant: fold statistics exist
    assert result.fold_statistics is not None
    assert 0.0 <= result.fold_statistics.pct_profitable_folds <= 100.0
    assert result.fold_statistics.best_fold_net_pnl >= result.fold_statistics.worst_fold_net_pnl


def test_insufficient_days_raises_value_error(wf_validator):
    """If available history is shorter than min_train_days + test_block_days, fail clearly."""
    df_short = _generate_multi_day_data(num_days=10)

    with pytest.raises(ValueError, match="Only 10 trading days available"):
        wf_validator.validate(
            df_15m=df_short,
            min_train_days=20,
            test_block_days=5,
        )
