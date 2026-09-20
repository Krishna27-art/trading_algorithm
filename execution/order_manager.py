"""
Order State Management & Duplicate Prevention Machine.
"""

from datetime import datetime
from typing import Dict, List, Optional
from database.models import OrderRecord, OrderStatus
from monitoring.logger import logger


class OrderManager:
    def __init__(self):
        self.orders: Dict[str, OrderRecord] = {}
        self.broker_id_map: Dict[str, str] = {}  # broker_order_id -> internal order_id

    def register_order(self, order: OrderRecord):
        self.orders[order.order_id] = order
        if order.broker_order_id:
            self.broker_id_map[order.broker_order_id] = order.order_id

    def update_order_status(
        self,
        order_id: str,
        status: OrderStatus,
        filled_qty: int = 0,
        fill_price: float = 0.0,
        reject_reason: Optional[str] = None,
    ) -> Optional[OrderRecord]:
        target_id = self.broker_id_map.get(order_id, order_id)
        if target_id not in self.orders:
            logger.warning(f"Order {order_id} not found in OrderManager.")
            return None

        order = self.orders[target_id]
        order.status = status
        order.filled_quantity = filled_qty
        order.average_fill_price = fill_price
        order.updated_at = datetime.now()
        if reject_reason:
            order.reject_reason = reject_reason

        logger.info(f"Order {target_id} state transition -> {status.value}")
        return order

    def get_active_orders(self, symbol: Optional[str] = None) -> List[OrderRecord]:
        active_statuses = {OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}
        results = []
        for o in self.orders.values():
            if o.status in active_statuses:
                if symbol is None or o.symbol == symbol:
                    results.append(o)
        return results

    def is_order_in_flight(self, symbol: str) -> bool:
        """Returns True if there is a pending/submitted order for the symbol."""
        for o in self.orders.values():
            if o.symbol == symbol and o.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED):
                return True
        return False
