"""
Unit tests for RiskManager pre-trade gate, daily loss circuit breaker, and trade limits.
"""

from datetime import date, time
import pytest
from config.settings import RiskConfig
from risk.risk_manager import RiskManager


@pytest.fixture
def risk_mgr():
    cfg = RiskConfig(
        initial_capital=1_000_000.0,
        risk_per_trade_pct=1.0,
        max_daily_loss_pct=0.02,
        max_trades_per_day=3,
        entry_window_start=time(9, 45),
        entry_window_end=time(13, 30),
    )
    rm = RiskManager(cfg)
    rm.reset_daily_state(date(2024, 9, 2))
    return rm


def test_trading_time_window_enforcement(risk_mgr):
    """Signals before 09:45 or after 13:30 must be rejected."""
    # Before 09:45
    ok, reason = risk_mgr.validate_pre_trade("NIFTY", time(9, 30), 25, 1_000_000.0)
    assert ok is False
    assert "outside entry window" in reason

    # Within window (10:15)
    ok, reason = risk_mgr.validate_pre_trade("NIFTY", time(10, 15), 25, 1_000_000.0)
    assert ok is True

    # After 13:30
    ok, reason = risk_mgr.validate_pre_trade("NIFTY", time(14, 0), 25, 1_000_000.0)
    assert ok is False
    assert "outside entry window" in reason


def test_max_trades_per_day_enforcement(risk_mgr):
    """Cannot take more than max_trades_per_day trades in a single session."""
    assert sum(risk_mgr.daily_trades_count.values()) == 0

    risk_mgr.record_trade_executed("NIFTY")
    risk_mgr.record_trade_executed("NIFTY")
    risk_mgr.record_trade_executed("NIFTY")
    assert sum(risk_mgr.daily_trades_count.values()) == 3

    # 4th trade attempt must be rejected
    ok, reason = risk_mgr.validate_pre_trade("NIFTY", time(10, 30), 25, 1_000_000.0)
    assert ok is False
    assert "Daily trade limit reached" in reason


def test_daily_loss_circuit_breaker(risk_mgr):
    """When daily loss reaches 2%, kill switch triggers and halts all trading."""
    # Loss of 15,000 on 1M capital is 1.5% -> no kill switch
    ks1 = risk_mgr.update_pnl(current_unrealized_pnl=-15000.0, capital=1_000_000.0)
    assert ks1 is False
    assert risk_mgr.kill_switch_active is False

    # Loss reaches 21,000 (2.1% > 2.0%) -> triggers kill switch
    ks2 = risk_mgr.update_pnl(current_unrealized_pnl=-21000.0, capital=1_000_000.0)
    assert ks2 is True
    assert risk_mgr.kill_switch_active is True

    # New trades blocked by kill switch
    ok, reason = risk_mgr.validate_pre_trade("NIFTY", time(11, 0), 25, 1_000_000.0)
    assert ok is False
    assert "kill-switch is active" in reason.lower()
