"""
Integration Tests for Event-Driven Backtesting and Walk-Forward Engine.
"""

from datetime import datetime
import pytest

from backtest.event_engine import EventDrivenBacktester
from backtest.walk_forward import WalkForwardValidator
from config.settings import InstrumentConfig, InstrumentType, settings
from data.historical_loader import HistoricalDataLoader


def test_event_driven_backtester_end_to_end():
    # Generate 30 days of synthetic Nifty data
    df = HistoricalDataLoader.generate_synthetic_nifty_data(
        start_date=datetime(2025, 1, 1),
        days=30,
        base_price=24000.0,
        seed=101,
    )

    inst = settings.instruments[0]
    backtester = EventDrivenBacktester(instrument=inst, app_settings=settings)
    report = backtester.run(df, initial_capital=1000000.0)

    # Validate output report structure
    assert report.total_trades >= 0
    assert report.total_transaction_costs >= 0
    assert isinstance(report.win_rate_pct, float)
    assert isinstance(report.profit_factor, float)
    assert isinstance(report.max_drawdown_pct, float)


def test_walk_forward_validation():
    df = HistoricalDataLoader.generate_synthetic_nifty_data(
        start_date=datetime(2025, 1, 1),
        days=40,
        base_price=24000.0,
        seed=202,
    )
    inst = settings.instruments[0]
    validator = WalkForwardValidator(instrument=inst, app_settings=settings)
    result = validator.validate(df, split_ratio=0.70, initial_capital=1000000.0)

    assert result.in_sample_report is not None
    assert result.out_of_sample_report is not None
    assert isinstance(result.profit_factor_retention_pct, float)


def test_rolling_walk_forward_validation():
    from backtest.rolling_walk_forward import RollingWalkForwardValidator

    df = HistoricalDataLoader.generate_synthetic_nifty_data(
        start_date=datetime(2025, 1, 1),
        days=90,
        base_price=24000.0,
        seed=303,
    )
    inst = settings.instruments[0]
    validator = RollingWalkForwardValidator(instrument=inst, app_settings=settings)
    result = validator.validate(
        df,
        min_train_days=50,
        test_block_days=15,
        initial_capital=1000000.0,
    )

    assert len(result.folds) > 0
    assert result.combined_out_of_sample_report is not None
    assert isinstance(result.final_capital, float)
    assert len(result.all_out_of_sample_trades) >= 0
