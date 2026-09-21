"""
Tests for Broker/API Failure Handling and Safe States.
Phase 9 Requirement:
- API timeout / network disconnect during order placement -> status UNKNOWN.
- UNKNOWN status blocks subsequent automated orders and requires reconciliation.
- Broker rejections fail cleanly without creating open positions.
- Safe state transitions: PENDING -> SUBMITTED -> OPEN -> FILLED / REJECTED / UNKNOWN.
"""

from datetime import datetime
from unittest.mock import MagicMock
import pytest
from broker.paper_broker import PaperBrokerAdapter
from config.settings import AppSettings, InstrumentConfig, RiskConfig, StrategyConfig
from database.db import DatabaseManager
from database.models import OrderDirection, OrderRecord, OrderStatus, OrderType
from execution.execution_engine import ExecutionEngine
from execution.order_manager import OrderManager
from strategy.base_strategy import SignalAction, StrategySignal


@pytest.fixture
def failure_engine(tmp_path):
    db_file = tmp_path / "failure_test.db"
    db = DatabaseManager(db_file)
    broker = PaperBrokerAdapter(initial_capital=500_000.0)
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
    engine = ExecutionEngine(
        broker=broker,
        instrument=instrument,
        app_settings=settings,
        db=db,
    )
    engine.start()
    return engine, broker, db


def test_api_timeout_during_order_placement_sets_unknown_state(failure_engine):
    """
    If broker throws Timeout / ConnectionError during place_order:
    1. An order record with status UNKNOWN is registered.
    2. Engine reconciliation is invalidated.
    3. Future orders are blocked until explicit reconciliation.
    """
    engine, broker, db = failure_engine
    broker.set_ltp("NIFTY24SEPFUT", 24500.0)

    # Mock broker.place_order to simulate network timeout
    broker.place_order = MagicMock(side_effect=TimeoutError("Kite API HTTP connection timeout"))

    signal = StrategySignal(
        action=SignalAction.BUY,
        symbol="NIFTY24SEPFUT",
        timestamp=datetime(2024, 9, 20, 10, 0),
        price=24500.0,
        stop_loss=24450.0,
        target=24600.0,
        signal_id="SIG_TIMEOUT_01",
    )

    with pytest.raises(TimeoutError):
        engine._execute_entry_signal(signal)

    # Invariant check: Engine must be in unreconciled error state
    assert engine.is_reconciled is False
    assert "UNKNOWN state" in engine.reconciliation_error
    assert engine.strategy.position == 0

    # Order manager must register UNKNOWN order
    active_orders = engine.order_manager.get_active_orders("NIFTY24SEPFUT")
    assert any(o.status == OrderStatus.UNKNOWN for o in active_orders)

    # Attempting second order must be blocked immediately
    signal2 = StrategySignal(
        action=SignalAction.BUY,
        symbol="NIFTY24SEPFUT",
        timestamp=datetime(2024, 9, 20, 10, 5),
        price=24510.0,
        stop_loss=24460.0,
        target=24610.0,
        signal_id="SIG_TIMEOUT_02",
    )
    engine._execute_entry_signal(signal2)
    # broker.place_order was NOT called a second time
    assert broker.place_order.call_count == 1


def test_order_rejected_by_broker_fails_safely(failure_engine):
    """If broker rejects an order (e.g. margin shortfall or rms reject), no position is created."""
    engine, broker, db = failure_engine
    broker.set_ltp("NIFTY24SEPFUT", 24500.0)

    rejected_order = OrderRecord(
        order_id="REJ_123",
        symbol="NIFTY24SEPFUT",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        price=24500.0,
        quantity=25,
        status=OrderStatus.REJECTED,
        reject_reason="RMS: Margin insufficient",
    )
    broker.place_order = MagicMock(return_value=rejected_order)

    signal = StrategySignal(
        action=SignalAction.BUY,
        symbol="NIFTY24SEPFUT",
        timestamp=datetime(2024, 9, 20, 10, 0),
        price=24500.0,
        stop_loss=24450.0,
        target=24600.0,
    )

    engine._execute_entry_signal(signal)

    # In case of rejection without fill, position must remain 0
    assert broker.positions.get("NIFTY24SEPFUT", 0) == 0


def test_state_transitions_in_order_manager():
    """Verify state transitions and terminal states in OrderManager."""
    om = OrderManager()
    order = OrderRecord(
        order_id="ORD_STATE_01",
        broker_order_id="BRK_STATE_01",
        symbol="NIFTY",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        price=24000.0,
        quantity=25,
        status=OrderStatus.PENDING,
    )
    om.register_order(order)
    assert om.orders["ORD_STATE_01"].status == OrderStatus.PENDING

    # Transition: PENDING -> SUBMITTED
    om.update_order_status("BRK_STATE_01", OrderStatus.SUBMITTED)
    assert om.orders["ORD_STATE_01"].status == OrderStatus.SUBMITTED

    # Transition: SUBMITTED -> OPEN
    om.update_order_status("BRK_STATE_01", OrderStatus.OPEN)
    assert om.orders["ORD_STATE_01"].status == OrderStatus.OPEN

    # Transition: OPEN -> PARTIALLY_FILLED
    om.update_order_status("BRK_STATE_01", OrderStatus.PARTIALLY_FILLED, filled_qty=10, fill_price=24000.0)
    assert om.orders["ORD_STATE_01"].status == OrderStatus.PARTIALLY_FILLED
    assert om.orders["ORD_STATE_01"].filled_quantity == 10

    # Transition: PARTIALLY_FILLED -> FILLED (terminal)
    om.update_order_status("BRK_STATE_01", OrderStatus.FILLED, filled_qty=25, fill_price=24000.0)
    assert om.orders["ORD_STATE_01"].status == OrderStatus.FILLED
    assert om.orders["ORD_STATE_01"].filled_quantity == 25

    # Invalid backward transition attempt must be rejected
    om.update_order_status("BRK_STATE_01", OrderStatus.OPEN)
    assert om.orders["ORD_STATE_01"].status == OrderStatus.FILLED
