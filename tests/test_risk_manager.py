"""
Unit Tests for Risk Management, Position Sizing, and Circuit Breaker.
"""

from datetime import date, time
import pytest

from config.settings import InstrumentConfig, InstrumentType, RiskConfig
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager


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

    # When OR width is 140 (> 120), stop distance of 110 is clamped to 80 points
    # Units = 10,000 / 80 = 125 units -> 5 lots -> 125 qty.
    qty = sizer.calculate_order_quantity(
        capital=1000000.0, stop_distance=110.0, instrument=inst, or_width=140.0
    )
    assert qty == 125


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


def test_execution_engine_realized_pnl_circuit_breaker():
    from datetime import datetime
    from broker.paper_broker import PaperBrokerAdapter
    from config.settings import settings
    from execution.execution_engine import ExecutionEngine
    from strategy.base_strategy import SignalAction, StrategySignal

    inst = settings.instruments[0]
    broker = PaperBrokerAdapter(initial_capital=1000000.0)
    engine = ExecutionEngine(broker=broker, instrument=inst, app_settings=settings)
    engine.start()

    # Simulate entering a trade
    entry_sig = StrategySignal(
        action=SignalAction.BUY,
        symbol=inst.symbol,
        timestamp=datetime(2026, 3, 2, 9, 45),
        price=24000.0,
        stop_loss=23900.0,
        target=24200.0,
        reason="ORB_BREAKOUT_LONG",
    )
    engine._execute_entry_signal(entry_sig)
    assert engine.current_trade is not None

    # Simulate exit with a large loss exceeding 2% (20k)
    exit_sig = StrategySignal(
        action=SignalAction.EXIT,
        symbol=inst.symbol,
        timestamp=datetime(2026, 3, 2, 10, 15),
        price=23700.0, # -300 pts * qty
        reason="STOP_LOSS",
    )
    engine._execute_exit_signal(exit_sig)

    # Risk manager must have recorded the realized loss and triggered kill-switch
    assert engine.risk_manager.daily_realized_pnl < -20000.0
    assert engine.risk_manager.kill_switch_active is True

