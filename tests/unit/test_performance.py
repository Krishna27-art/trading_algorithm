"""
Unit tests for Performance Metrics & Golden Dataset Verification.
"""

from datetime import datetime, date
import pytest
import numpy as np
from backtest.performance import PerformanceAnalyzer, PerformanceReport


def test_performance_metrics_golden_dataset():
    """
    TEST_PERFORMANCE_METRICS_GOLDEN_DATASET
    Deterministic, hand-calculated 4-trade dataset verifying:
    P&L, win rate, profit factor, drawdown, expectancy, R, cost drag.
    """
    initial_capital = 100_000.0

    golden_trades = [
        {
            "entry_time": datetime(2024, 1, 1, 9, 30),
            "direction": "BUY",
            "pnl_gross": 2500.0,
            "total_costs": 100.0,
            "pnl_net": 2400.0,
            "r_multiple": 2.4,
        },
        {
            "entry_time": datetime(2024, 1, 2, 9, 30),
            "direction": "SELL",
            "pnl_gross": -1100.0,
            "total_costs": 100.0,
            "pnl_net": -1200.0,
            "r_multiple": -1.2,
        },
        {
            "entry_time": datetime(2024, 1, 3, 9, 30),
            "direction": "BUY",
            "pnl_gross": 3100.0,
            "total_costs": 100.0,
            "pnl_net": 3000.0,
            "r_multiple": 3.0,
        },
        {
            "entry_time": datetime(2024, 1, 4, 9, 30),
            "direction": "BUY",
            "pnl_gross": -700.0,
            "total_costs": 100.0,
            "pnl_net": -800.0,
            "r_multiple": -0.8,
        },
    ]

    report = PerformanceAnalyzer.generate_report(
        trades=golden_trades,
        initial_capital=initial_capital,
        risk_free_rate=0.065,
    )

    # Hand-calculated assertions
    assert report.total_trades == 4
    assert report.winning_trades == 2
    assert report.losing_trades == 2
    assert report.win_rate_pct == 50.0
    assert report.gross_pnl == 3800.0
    assert report.total_transaction_costs == 400.0
    assert report.net_pnl == 3400.0
    assert report.cost_drag_pct == 10.53

    # Profit Factor: gross wins (5400) / gross losses (2000) = 2.70
    assert report.profit_factor == 2.70

    # Expectancy: 3400 net / 4 trades = 850.0
    assert report.expectancy_rupees == 850.0

    # Average R: (2.4 - 1.2 + 3.0 - 0.8) / 4 = 3.4 / 4 = 0.85
    assert pytest.approx(report.avg_r_multiple, rel=1e-4) == 0.85

    # Max Drawdown: Peak after T1 is 102,400. After T2 is 101,200. Loss is 1,200 / 102,400 = 1.171875%
    # Peak after T3 is 104,200. After T4 is 103,400. Loss is 800 / 104,200 = 0.7677%
    assert report.max_drawdown_pct == 1.17

    # Max consecutive losses = 1
    assert report.max_consecutive_losses == 1


def test_profit_factor_edge_cases():
    """Verify zero losses and zero wins handle profit factor cleanly without ZeroDivisionError."""
    # Zero losses
    all_wins = [
        {
            "entry_time": datetime(2024, 1, 1, 9, 30),
            "direction": "BUY",
            "pnl_gross": 1000.0,
            "total_costs": 50.0,
            "pnl_net": 950.0,
            "r_multiple": 1.5,
        }
    ]
    rep_wins = PerformanceAnalyzer.generate_report(all_wins)
    assert rep_wins.profit_factor == float("inf")

    # Zero wins
    all_losses = [
        {
            "entry_time": datetime(2024, 1, 1, 9, 30),
            "direction": "BUY",
            "pnl_gross": -1000.0,
            "total_costs": 50.0,
            "pnl_net": -1050.0,
            "r_multiple": -1.0,
        }
    ]
    rep_losses = PerformanceAnalyzer.generate_report(all_losses)
    assert rep_losses.profit_factor == 0.0

    # Empty
    rep_empty = PerformanceAnalyzer.generate_report([])
    assert rep_empty.profit_factor == 0.0
    assert rep_empty.total_trades == 0


def test_sharpe_with_all_trading_dates_zero_return_days():
    """
    Sharpe calculation must include non-trading days across the requested backtest period
    rather than only active trading days, preventing distortion.
    """
    initial_capital = 100_000.0
    trade = [
        {
            "entry_time": datetime(2024, 1, 1, 9, 30),
            "direction": "BUY",
            "pnl_gross": 1000.0,
            "total_costs": 50.0,
            "pnl_net": 950.0,
            "r_multiple": 1.0,
        }
    ]

    all_dates = [
        date(2024, 1, 1),
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
        date(2024, 1, 5),
    ]

    rep_full = PerformanceAnalyzer.generate_report(
        trade,
        initial_capital=initial_capital,
        all_trading_dates=all_dates,
    )

    # Returns series is [950/100000, 0, 0, 0, 0] = [0.0095, 0, 0, 0, 0]
    rets = np.array([0.0095, 0.0, 0.0, 0.0, 0.0])
    mean_r = rets.mean()
    std_r = rets.std(ddof=1)
    expected_sharpe = ((mean_r * 252) - 0.065) / (std_r * np.sqrt(252))

    assert pytest.approx(rep_full.sharpe_ratio, rel=1e-3) == expected_sharpe


def test_cagr_uses_full_backtest_duration():
    """CAGR must respect full backtest period if specified."""
    trade = [
        {
            "entry_time": datetime(2024, 1, 1, 9, 30),
            "direction": "BUY",
            "pnl_gross": 10000.0,
            "total_costs": 0.0,
            "pnl_net": 10000.0,
            "r_multiple": 1.0,
        }
    ]
    # 1 year period
    rep = PerformanceAnalyzer.generate_report(
        trade,
        initial_capital=100_000.0,
        backtest_start_date=datetime(2024, 1, 1),
        backtest_end_date=datetime(2025, 1, 1),
    )
    # 100k -> 110k over 1 year is approx 10%
    assert pytest.approx(rep.cagr_pct, abs=0.2) == 10.0
