"""
Tests for Duplicate Order Prevention and Signal Idempotency.
Phase 7 Requirement:
- SEND SAME ORDER TWICE test: Expectation: ONE ACTIVE ORDER / POSITION.
- Same signal repeated on consecutive ticks.
- Duplicate callback / status update idempotency.
"""

from datetime import datetime
import pytest
from broker.paper_broker import PaperBrokerAdapter
from config.settings import AppSettings, InstrumentConfig, RiskConfig, StrategyConfig
from database.db import DatabaseManager
from database.models import OrderDirection, OrderRecord, OrderStatus, OrderType
from execution.execution_engine import ExecutionEngine
from execution.order_manager import DuplicateOrderException, OrderManager
from strategy.base_strategy import SignalAction, StrategySignal


@pytest.fixture
def clean_engine(tmp_path):
    db_file = tmp_path / "test_exec.db"
    db = DatabaseManager(db_file)
    broker = PaperBrokerAdapter(initial_capital=500_000.0, slippage_points=0.0)
    broker.connect()
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


def test_send_same_order_twice(clean_engine):
    """
    SEND SAME ORDER TWICE
    Expected result: ONE ACTIVE ORDER / POSITION.
    """
    engine, broker, db = clean_engine
    broker.set_ltp("NIFTY24SEPFUT", 24500.0)

    # Handcraft a signal
    signal = StrategySignal(
        action=SignalAction.BUY,
        symbol="NIFTY24SEPFUT",
        timestamp=datetime(2024, 9, 20, 10, 0),
        price=24500.0,
        stop_loss=24450.0,
        target=24600.0,
        reason="ORB_BULLISH_BREAKOUT",
    )

    # 1. Execute signal first time
    engine._execute_entry_signal(signal)

    # Verify initial position
    positions = broker.get_positions()
    assert len(positions) == 1
    initial_qty = positions[0]["quantity"]
    assert initial_qty > 0
    assert engine.strategy.position == 1

    # 2. Re-send the exact same signal
    engine._execute_entry_signal(signal)

    # Verify that no duplicate order or position was added
    positions_after = broker.get_positions()
    assert len(positions_after) == 1
    assert positions_after[0]["quantity"] == initial_qty
    assert engine.strategy.position == 1

    # Database check: Only 1 trade entry was recorded
    trades = db.get_open_trades("NIFTY24SEPFUT")
    assert len(trades) == 1


def test_same_signal_repeated_consecutive_ticks(clean_engine):
    """Signals repeated over consecutive ticks must be filtered out idempotently."""
    engine, broker, db = clean_engine
    broker.set_ltp("NIFTY24SEPFUT", 24500.0)

    signal = StrategySignal(
        action=SignalAction.BUY,
        symbol="NIFTY24SEPFUT",
        timestamp=datetime(2024, 9, 20, 10, 0),
        price=24500.0,
        stop_loss=24450.0,
        target=24600.0,
        signal_id="SIG_DETERMINISTIC_101",
    )

    engine._execute_entry_signal(signal)
    first_trade = engine.current_trade.trade_id

    # Consecutive tick fires same signal ID
    engine._execute_entry_signal(signal)
    assert engine.current_trade.trade_id == first_trade
    assert len(broker.get_positions()) == 1


def test_order_manager_duplicate_client_order_id():
    """OrderManager must raise DuplicateOrderException when registering same client_order_id."""
    om = OrderManager()
    order1 = OrderRecord(
        order_id="ORD_1",
        client_order_id="CLT_999",
        symbol="NIFTY",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        price=24000.0,
        quantity=25,
    )
    om.register_order(order1)

    order2 = OrderRecord(
        order_id="ORD_2",
        client_order_id="CLT_999",
        symbol="NIFTY",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        price=24000.0,
        quantity=25,
    )
    with pytest.raises(DuplicateOrderException):
        om.register_order(order2)


def test_duplicate_callback_idempotency():
    """Duplicate status update callbacks must be idempotent and preserve terminal states."""
    om = OrderManager()
    order = OrderRecord(
        order_id="ORD_10",
        broker_order_id="BRK_10",
        symbol="NIFTY",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        price=24000.0,
        quantity=25,
        status=OrderStatus.OPEN,
    )
    om.register_order(order)

    # 1. Fill order
    upd1 = om.update_order_status("BRK_10", OrderStatus.FILLED, filled_qty=25, fill_price=24005.0)
    assert upd1.status == OrderStatus.FILLED
    assert upd1.filled_quantity == 25

    # 2. Duplicate callback arrives with same fill
    upd2 = om.update_order_status("BRK_10", OrderStatus.FILLED, filled_qty=25, fill_price=24005.0)
    assert upd2.status == OrderStatus.FILLED
    assert upd2.filled_quantity == 25

    # 3. Belated out-of-order SUBMITTED or OPEN callback must NOT revert terminal FILLED state
    upd3 = om.update_order_status("BRK_10", OrderStatus.OPEN, filled_qty=0, fill_price=0.0)
    assert upd3.status == OrderStatus.FILLED
