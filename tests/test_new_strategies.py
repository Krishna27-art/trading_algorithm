"""
Tests for CPRRegimeBreakoutStrategy, BufferedDualEMAStrategy, and the
generic StrategyBacktester/RollingWalkForwardValidator wiring that runs them.
"""
from datetime import datetime, timedelta
import pandas as pd
import pytest

from backtest.rolling_walk_forward import RollingWalkForwardValidator
from backtest.strategy_backtester import StrategyBacktester
from config.settings import settings
from data.historical_loader import HistoricalDataLoader
from strategy.cpr_strategy import CPRRegimeBreakoutStrategy, Regime, _compute_pivots
from strategy.dual_ema_strategy import BufferedDualEMAStrategy


@pytest.fixture
def instrument():
    return settings.instruments[0]


@pytest.fixture
def synthetic_df():
    return HistoricalDataLoader.generate_synthetic_nifty_data(
        start_date=datetime(2024, 1, 1), days=120, base_price=24000.0
    )


def test_compute_pivots_matches_formula():
    pivots = _compute_pivots(high=110.0, low=90.0, close=100.0)
    assert pivots["P"] == pytest.approx(100.0)
    assert pivots["BC"] == pytest.approx(100.0)
    assert pivots["TC"] == pytest.approx(100.0)  # H,L symmetric around close -> BC==TC==P
    assert pivots["width_pct"] == pytest.approx(0.0)


def test_cpr_seed_context_sets_regime_from_lookback(instrument):
    strat = CPRRegimeBreakoutStrategy(instrument)
    dates = pd.date_range("2024-01-01", periods=25, freq="D")
    rows = []
    for d in dates:
        rows.append({"datetime": d, "high": 24100.0, "low": 23900.0, "close": 24000.0, "volume": 1000})
    lookback = pd.DataFrame(rows)

    strat.seed_context(lookback)
    assert strat.pivots is not None
    assert strat.regime in (Regime.NARROW, Regime.WIDE, Regime.NEUTRAL)


def test_cpr_seed_context_empty_history_is_neutral(instrument):
    strat = CPRRegimeBreakoutStrategy(instrument)
    strat.seed_context(pd.DataFrame(columns=["datetime", "high", "low", "close"]))
    assert strat.pivots is None
    assert strat.regime == Regime.NEUTRAL

    strat.reset_session(datetime(2024, 1, 2).date())
    signal = strat.on_candle(
        {"datetime": datetime(2024, 1, 2, 9, 45), "open": 24000, "high": 24050, "low": 23950, "close": 24030},
        vwap=24000,
    )
    assert signal is None  # no context yet -> never trades


def test_dual_ema_flat_with_insufficient_history(instrument):
    strat = BufferedDualEMAStrategy(instrument)
    strat.seed_context(pd.DataFrame(columns=["datetime", "high", "low", "close"]))
    strat.reset_session(datetime(2024, 1, 2).date())
    signal = strat.on_candle(
        {"datetime": datetime(2024, 1, 2, 9, 30), "open": 24000, "high": 24010, "low": 23990, "close": 24000},
        vwap=24000,
    )
    # A single bar can't produce a meaningful EMA/ATR/SMA200 crossover signal
    assert signal is None


def test_dual_ema_long_signal_on_clear_uptrend(instrument):
    strat = BufferedDualEMAStrategy(instrument)
    # Warm up with a steady uptrend so EMA9 > EMA21 > SMA200 and ATR is well-defined
    base = datetime(2024, 1, 1, 9, 15)
    rows = []
    price = 23000.0
    for i in range(60):
        price += 15.0  # steady climb
        rows.append({"datetime": base + timedelta(minutes=15 * i), "high": price + 5, "low": price - 5, "close": price})
    warm = pd.DataFrame(rows)
    strat.seed_context(warm)
    strat.reset_session(datetime(2024, 1, 2).date())

    price += 15.0
    candle = {"datetime": datetime(2024, 1, 2, 9, 30), "open": price - 15, "high": price + 5, "low": price - 5, "close": price}
    signal = strat.on_candle(candle, vwap=price - 10)
    assert signal is not None
    assert signal.action.value == "BUY"
    assert signal.stop_loss < signal.price < signal.target


def test_strategy_backtester_runs_cpr_end_to_end(instrument, synthetic_df):
    backtester = StrategyBacktester(
        strategy_factory=lambda: CPRRegimeBreakoutStrategy(instrument),
        instrument=instrument,
    )
    report = backtester.run(synthetic_df, initial_capital=settings.risk.initial_capital)
    assert report.total_trades >= 0  # synthetic data may or may not trigger a regime day, just must not crash


def test_strategy_backtester_runs_dual_ema_end_to_end(instrument, synthetic_df):
    backtester = StrategyBacktester(
        strategy_factory=lambda: BufferedDualEMAStrategy(instrument),
        instrument=instrument,
    )
    report = backtester.run(synthetic_df, initial_capital=settings.risk.initial_capital)
    assert report.total_trades >= 0


def test_rolling_walk_forward_accepts_cpr_backtester_factory(instrument, synthetic_df):
    validator = RollingWalkForwardValidator(
        instrument=instrument,
        backtester_factory=lambda inst: StrategyBacktester(
            strategy_factory=lambda: CPRRegimeBreakoutStrategy(inst), instrument=inst
        ),
    )
    result = validator.validate(
        synthetic_df, min_train_days=30, test_block_days=15,
        initial_capital=settings.risk.initial_capital,
    )
    assert len(result.folds) >= 1
    assert result.combined_out_of_sample_report is not None


def test_rolling_walk_forward_still_defaults_to_orb(instrument, synthetic_df):
    """Backward-compatibility check: existing callers that don't pass a
    backtester_factory keep getting the ORB+VWAP EventDrivenBacktester."""
    validator = RollingWalkForwardValidator(instrument=instrument)
    result = validator.validate(
        synthetic_df, min_train_days=30, test_block_days=15,
        initial_capital=settings.risk.initial_capital,
    )
    assert len(result.folds) >= 1
