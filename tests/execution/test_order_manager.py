"""
Unit tests for OrderManager state tracking, ID mapping, and in-flight detection.
"""

from datetime import datetime
import pytest
from database.models import OrderDirection, OrderRecord, OrderStatus, OrderType
from execution.order_manager import DuplicateOrderException, OrderManager


def test_order_registration_and_id_mappings():
    """Verify OrderManager maps internal order_id, broker_order_id, and client_order_id correctly."""
    om = OrderManager()
    order = OrderRecord(
        order_id="INT_01",
        broker_order_id="BRK_12345",
        client_order_id="CLT_ABCDE",
        symbol="NIFTY",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        price=24000.0,
        quantity=25,
        status=OrderStatus.PENDING,
    )
    om.register_order(order)

    assert "INT_01" in om.orders
    assert om.broker_id_map.get("BRK_12345") == "INT_01"
    assert om.client_id_map.get("CLT_ABCDE") == "INT_01"
    assert om.get_order_by_client_id("CLT_ABCDE") == order


def test_is_order_in_flight():
    """Pending, submitted, and unknown state orders are identified as in-flight."""
    om = OrderManager()
    assert om.is_order_in_flight("NIFTY") is False

    order = OrderRecord(
        order_id="INT_02",
        symbol="NIFTY",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        price=24000.0,
        quantity=25,
        status=OrderStatus.SUBMITTED,
    )
    om.register_order(order)
    assert om.is_order_in_flight("NIFTY") is True
    assert om.is_order_in_flight("RELIANCE") is False

    # Once filled, no longer in flight
    om.update_order_status("INT_02", OrderStatus.FILLED, filled_qty=25, fill_price=24000.0)
    assert om.is_order_in_flight("NIFTY") is False


def test_get_active_orders():
    """Fetches active resting orders, excluding filled, cancelled, or rejected."""
    om = OrderManager()
    o1 = OrderRecord(order_id="O1", symbol="NIFTY", direction=OrderDirection.BUY, order_type=OrderType.LIMIT, price=24000.0, quantity=25, status=OrderStatus.OPEN)
    o2 = OrderRecord(order_id="O2", symbol="NIFTY", direction=OrderDirection.SELL, order_type=OrderType.LIMIT, price=24100.0, quantity=25, status=OrderStatus.FILLED)
    o3 = OrderRecord(order_id="O3", symbol="RELIANCE", direction=OrderDirection.BUY, order_type=OrderType.LIMIT, price=2900.0, quantity=10, status=OrderStatus.OPEN)

    om.register_order(o1)
    om.register_order(o2)
    om.register_order(o3)

    active_nifty = om.get_active_orders("NIFTY")
    assert len(active_nifty) == 1
    assert active_nifty[0].order_id == "O1"

    all_active = om.get_active_orders()
    assert len(all_active) == 2
