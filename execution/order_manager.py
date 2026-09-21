"""
Order State Management & Duplicate Prevention Machine.
"""

from datetime import datetime
from typing import Dict, List, Optional, Set
from database.models import OrderRecord, OrderStatus
from monitoring.logger import logger


class DuplicateOrderException(Exception):
    """Raised when an order with an identical client_order_id or signal_id is submitted."""
    pass


class OrderManager:
    def __init__(self):
        self.orders: Dict[str, OrderRecord] = {}
        self.broker_id_map: Dict[str, str] = {}  # broker_order_id -> internal order_id
        self.client_id_map: Dict[str, str] = {}  # client_order_id -> internal order_id
        self.processed_signals: Set[str] = set()

    def is_duplicate_signal(self, signal_id: Optional[str]) -> bool:
        """Returns True if this strategy signal has already triggered an order."""
        if not signal_id:
            return False
        return signal_id in self.processed_signals

    def mark_signal_processed(self, signal_id: Optional[str]):
        if signal_id:
            self.processed_signals.add(signal_id)

    def is_duplicate_order(self, client_order_id: Optional[str]) -> bool:
        """Returns True if an order with this client_order_id is already registered."""
        if not client_order_id:
            return False
        return client_order_id in self.client_id_map

    def get_order_by_client_id(self, client_order_id: str) -> Optional[OrderRecord]:
        internal_id = self.client_id_map.get(client_order_id)
        if internal_id:
            return self.orders.get(internal_id)
        return None

    def register_order(self, order: OrderRecord):
        if order.client_order_id and order.client_order_id in self.client_id_map:
            logger.warning(f"Duplicate client_order_id: {order.client_order_id}. Order rejected.")
            raise DuplicateOrderException(f"Order with client_order_id '{order.client_order_id}' already registered.")

        self.orders[order.order_id] = order
        if order.client_order_id:
            self.client_id_map[order.client_order_id] = order.order_id
        if order.broker_order_id:
            self.broker_id_map[order.broker_order_id] = order.order_id
        if order.signal_id:
            self.processed_signals.add(order.signal_id)

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

        # Duplicate callback idempotency: if already in target status with same filled_qty, no-op
        if order.status == status and order.filled_quantity == filled_qty:
            logger.debug(f"Ignoring duplicate status update for order {target_id} in state {status.value}")
            return order

        # Terminal state protection: if already FILLED, CANCELLED, or REJECTED, do not revert to active states
        terminal_statuses = {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
        if order.status in terminal_statuses and status not in terminal_statuses:
            logger.warning(f"Rejecting invalid state transition for {target_id} from {order.status.value} to {status.value}")
            return order

        order.status = status
        order.filled_quantity = filled_qty
        order.average_fill_price = fill_price
        order.updated_at = datetime.now()
        if reject_reason:
            order.reject_reason = reject_reason

        logger.info(f"Order {target_id} state transition -> {status.value}")
        return order

    def get_active_orders(self, symbol: Optional[str] = None) -> List[OrderRecord]:
        active_statuses = {
            OrderStatus.PENDING,
            OrderStatus.SUBMITTED,
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.UNKNOWN,
        }
        results = []
        for o in self.orders.values():
            if o.status in active_statuses:
                if symbol is None or o.symbol == symbol:
                    results.append(o)
        return results

    def is_order_in_flight(self, symbol: str) -> bool:
        """Returns True if there is a pending, submitted, or unknown status order for the symbol."""
        for o in self.orders.values():
            if o.symbol == symbol and o.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.UNKNOWN):
                return True
        return False
