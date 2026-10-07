"""
Unit Tests for Risk Management, Position Sizing, and Circuit Breaker.
"""

from datetime import date, time
import pytest

from backend.config.settings import InstrumentConfig, InstrumentType, RiskConfig
from backend.backtest import PositionSizer
from backend.backtest import RiskManager


def test_position_sizing_futures_lot_rounding():
    risk_cfg = RiskConfig(initial_capital=1000000.0, risk_per_trade_pct=0.01) # ₹10,000 risk budget
    sizer = PositionSizer(risk_cfg)

    inst = InstrumentConfig(
        symbol="NIFTY",
        exchange="NFO",
        instrument_type=InstrumentType.FUTURES,
        lot_size=25,
        min_orb_range=40.0,
        max_orb_range=120.0,
        max_risk_cap=80.0,
    )

    # Stop distance = 50 pts. Units = 10,000 / 50 = 200 units. Lots = 200 / 25 = 8 lots -> 200 qty.
    qty = sizer.calculate_order_quantity(capital=1000000.0, stop_distance=50.0, instrument=inst)
    assert qty == 200

    # Stop distance = 65 pts. Units = 10,000 / 65 = 153.8 units. Lots = floor(153.8 / 25) = 6 lots -> 150 qty.
    qty2 = sizer.calculate_order_quantity(capital=1000000.0, stop_distance=65.0, instrument=inst)
    assert qty2 == 150


def test_position_sizing_with_risk_cap():
    risk_cfg = RiskConfig(initial_capital=1000000.0, risk_per_trade_pct=0.01)
    sizer = PositionSizer(risk_cfg)

    inst = InstrumentConfig(
        symbol="NIFTY",
        exchange="NFO",
        instrument_type=InstrumentType.FUTURES,
        lot_size=25,
        min_orb_range=40.0,
        max_orb_range=120.0,
        max_risk_cap=80.0,
    )

    # Under Phase 4 true economic risk rule:
    # Stop distance of 110 is sized using actual economic stop: 10,000 / 110 = 90.9 units -> 3 lots -> 75 qty.
    qty = sizer.calculate_order_quantity(
        capital=1000000.0, stop_distance=110.0, instrument=inst, or_width=140.0
    )
    assert qty == 75


def test_daily_kill_switch_circuit_breaker():
    risk_cfg = RiskConfig(initial_capital=1000000.0, max_daily_loss_pct=0.02) # 2% = ₹20,000 max loss
    rm = RiskManager(risk_cfg)
    rm.reset_daily_state(date(2026, 3, 2))

    # Loss ₹15,000 (< ₹20,000) -> Not triggered
    rm.update_pnl(realized_pnl_delta=-15000.0, capital=1000000.0)
    assert rm.kill_switch_active is False

    # Loss reaches -₹21,000 (>= ₹20,000) -> Circuit breaker triggered!
    rm.update_pnl(realized_pnl_delta=-6000.0, capital=1000000.0)
    assert rm.kill_switch_active is True

    # Subsequent pre-trade check is rejected
    approved, reason = rm.validate_pre_trade(
        symbol="NIFTY", current_time=time(10, 30), quantity=25, capital=1000000.0
    )
    assert approved is False
    assert "kill-switch" in reason


def test_max_one_trade_per_day():
    rm = RiskManager()
    rm.reset_daily_state(date(2026, 3, 2))

    # First trade allowed
    approved, _ = rm.validate_pre_trade(
        symbol="NIFTY", current_time=time(10, 0), quantity=25, capital=1000000.0
    )
    assert approved is True

    # Register execution
    rm.record_trade_executed("NIFTY")

    # Second trade rejected
    approved2, reason = rm.validate_pre_trade(
        symbol="NIFTY", current_time=time(11, 0), quantity=25, capital=1000000.0
    )
    assert approved2 is False
    assert "limit reached" in reason


def test_portfolio_wide_one_trade_per_day_across_symbols():
    """
    Verifies that the 1-trade-per-day limit applies across ALL instruments combined,
    not per-symbol. When RELIANCE trades, TCS must be rejected.
    """
    rm = RiskManager(max_portfolio_daily_trades=1)
    rm.reset_daily_state(date(2026, 3, 2))

    # Pre-trade check for RELIANCE is approved
    approved, _ = rm.validate_pre_trade(
        symbol="RELIANCE", current_time=time(10, 0), quantity=50, capital=1000000.0
    )
    assert approved is True

    # RELIANCE executes 1 trade
    rm.record_trade_executed("RELIANCE")

    # Now TCS must be REJECTED because the portfolio has reached its 1-trade quota
    approved_tcs, reason_tcs = rm.validate_pre_trade(
        symbol="TCS", current_time=time(10, 15), quantity=30, capital=1000000.0
    )
    assert approved_tcs is False
    assert "Portfolio" in reason_tcs or "portfolio" in reason_tcs
    assert "limit reached" in reason_tcs



