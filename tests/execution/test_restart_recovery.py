"""
Tests for Restart Recovery and Startup Reconciliation.
Phase 8 Requirement:
- Simulation: 09:50 position opened -> 09:55 crash -> 10:05 restart.
- System must never restart thinking 'no position' when broker position exists.
- Local state exists, broker position exists.
- Local state missing, broker position exists.
- Local state exists, broker position missing.
- Uncertain state blocks new orders until resolved.
"""

from datetime import datetime
import pytest
from broker.paper_broker import PaperBrokerAdapter
from config.settings import AppSettings, InstrumentConfig, RiskConfig, StrategyConfig
from database.db import DatabaseManager
from database.models import ExitReason, OrderDirection, OrderRecord, OrderStatus, OrderType, TradeRecord
from execution.execution_engine import ExecutionEngine
from strategy.base_strategy import SignalAction, StrategySignal


@pytest.fixture
def test_setup(tmp_path):
    db_file = tmp_path / "recovery_test.db"
    db = DatabaseManager(db_file)
    broker = PaperBrokerAdapter(initial_capital=500_000.0, slippage_points=0.0)
    instrument = InstrumentConfig(
        symbol="NIFTY24SEPFUT",
        exchange="NFO",
        instrument_type="FUTURES",
        lot_size=25,
        instrument_token=12345,
    )
    settings = AppSettings(
        risk=RiskConfig(initial_capital=500_000.0, risk_per_trade_pct=1.0, max_trades_per_day=5),
        strategy=StrategyConfig(),
        db_path=db_file,
    )
    return db_file, db, broker, instrument, settings


def test_crash_and_restart_recovers_position(test_setup):
    """
    Simulates:
    09:50 position opened
    09:55 application crashes
    10:05 application restarts
    Verifies that open position, trade ID, entry price, SL, and strategy state are restored.
    """
    db_file, db, broker, instrument, settings = test_setup

    # 1. Start original engine instance (09:50)
    engine_v1 = ExecutionEngine(
        broker=broker,
        instrument=instrument,
        app_settings=settings,
        db=db,
    )
    engine_v1.start()
    broker.set_ltp("NIFTY24SEPFUT", 24500.0)

    signal = StrategySignal(
        action=SignalAction.BUY,
        symbol="NIFTY24SEPFUT",
        timestamp=datetime(2024, 9, 20, 9, 50),
        price=24500.0,
        stop_loss=24450.0,
        target=24600.0,
        reason="ORB_BULLISH_BREAKOUT",
    )
    engine_v1._execute_entry_signal(signal)

    assert engine_v1.strategy.position == 1
    assert engine_v1.current_trade is not None
    orig_trade_id = engine_v1.current_trade.trade_id
    orig_entry_price = engine_v1.current_trade.entry_price

    # 2. Simulate 09:55 crash (discard engine_v1 without graceful shutdown)
    del engine_v1

    # 3. Simulate 10:05 restart: Instantiate a brand new ExecutionEngine pointing to same broker & DB
    engine_v2 = ExecutionEngine(
        broker=broker,
        instrument=instrument,
        app_settings=settings,
        db=db,
    )
    engine_v2.start()

    # System must NOT think 'no position'
    assert engine_v2.strategy.position == 1
    assert engine_v2.current_trade is not None
    assert engine_v2.current_trade.trade_id == orig_trade_id
    assert engine_v2.current_trade.entry_price == orig_entry_price
    assert engine_v2.is_reconciled is True
    assert engine_v2.reconciliation_error is None


def test_broker_position_exists_local_state_missing(test_setup):
    """
    If broker holds a position (e.g. executed externally or local DB lost),
    startup reconciliation must adopt it and create local trade tracking.
    """
    db_file, db, broker, instrument, settings = test_setup

    # Broker has position
    broker.positions["NIFTY24SEPFUT"] = 25
    broker.position_entry_prices["NIFTY24SEPFUT"] = 24500.0

    # Local DB has NO open trade
    assert len(db.get_open_trades("NIFTY24SEPFUT")) == 0

    engine = ExecutionEngine(
        broker=broker,
        instrument=instrument,
        app_settings=settings,
        db=db,
    )
    report = engine.reconcile_startup_state()

    assert report["action_taken"] == "RECOVERED_FROM_BROKER"
    assert engine.strategy.position == 1
    assert engine.current_trade is not None
    assert engine.current_trade.quantity == 25
    assert engine.current_trade.entry_price == 24500.0


def test_local_trade_exists_broker_position_missing(test_setup):
    """
    If local DB thinks trade is open, but broker position is 0 (closed offline),
    startup reconciliation closes local trade to prevent ghost position.
    """
    db_file, db, broker, instrument, settings = test_setup

    # Insert open trade into local DB
    ghost_trade = TradeRecord(
        trade_id="TRD_GHOST_1",
        symbol="NIFTY24SEPFUT",
        direction=OrderDirection.BUY,
        entry_time=datetime(2024, 9, 20, 9, 50),
        entry_price=24500.0,
        quantity=25,
        initial_stop=24400.0,
        initial_target=24700.0,
    )
    db.record_trade_entry(ghost_trade)

    # Broker position is 0
    broker.positions["NIFTY24SEPFUT"] = 0

    engine = ExecutionEngine(
        broker=broker,
        instrument=instrument,
        app_settings=settings,
        db=db,
    )
    report = engine.reconcile_startup_state()

    assert report["action_taken"] == "CLOSED_LOCAL_GHOST_TRADE"
    assert engine.strategy.position == 0
    assert engine.current_trade is None

    # Verify DB trade marked closed
    open_trades = db.get_open_trades("NIFTY24SEPFUT")
    assert len(open_trades) == 0


def test_uncertain_state_blocks_new_orders(test_setup):
    """If broker state is uncertain or reconciliation failed, all new orders must be blocked (Invariant 11)."""
    db_file, db, broker, instrument, settings = test_setup

    engine = ExecutionEngine(
        broker=broker,
        instrument=instrument,
        app_settings=settings,
        db=db,
    )
    # Mark reconciliation as failed
    engine.is_reconciled = False
    engine.reconciliation_error = "Broker API unreachable"

    signal = StrategySignal(
        action=SignalAction.BUY,
        symbol="NIFTY24SEPFUT",
        timestamp=datetime(2024, 9, 20, 10, 0),
        price=24500.0,
        stop_loss=24450.0,
        target=24600.0,
    )

    engine._execute_entry_signal(signal)

    # No order should have been placed
    assert len(broker.get_positions()) == 0
    assert engine.strategy.position == 0
