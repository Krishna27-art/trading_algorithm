"""
Realistic Paper Trading Execution Adapter.
Simulates fills, slippage, order tracking, and account margins in memory without real money risk.
"""

import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from broker.base_broker import BaseBrokerAdapter
from database.models import OrderDirection, OrderRecord, OrderStatus, OrderType
from monitoring.logger import logger


class PaperBrokerAdapter(BaseBrokerAdapter):
    def __init__(self, initial_capital: float = 1000000.0, slippage_points: float = 0.50):
        super().__init__(name="PAPER_BROKER")
        self.capital: float = initial_capital
        self.available_margin: float = initial_capital
        self.slippage_points = slippage_points
        self.orders: Dict[str, OrderRecord] = {}
        self.positions: Dict[str, int] = {}
        self.position_entry_prices: Dict[str, float] = {}
        self.last_ltp: Dict[str, float] = {}
        self.tick_callback: Optional[Callable[[float, int, datetime], None]] = None

    def connect(self) -> bool:
        self.is_connected = True
        logger.info(f"[{self.name}] Paper broker simulator connected. Available capital: ₹{self.capital:,.2f}")
        return True

    def disconnect(self):
        self.is_connected = False
        logger.info(f"[{self.name}] Paper broker simulator disconnected.")

    def set_ltp(self, symbol: str, price: float):
        self.last_ltp[symbol] = price

    def place_order(
        self,
        symbol: str,
        direction: OrderDirection,
        order_type: OrderType,
        quantity: int,
        price: Optional[float] = None,
        tag: Optional[str] = "algo",
    ) -> OrderRecord:
        order_id = f"paper_{uuid.uuid4().hex[:8]}"
        created_at = datetime.now()

        # Realistic Slippage adjustment
        ref_price = price if price is not None else self.last_ltp.get(symbol, 24000.0)
        if direction == OrderDirection.BUY:
            fill_price = ref_price + self.slippage_points
        else:
            fill_price = ref_price - self.slippage_points

        # Fill order immediately in paper mode
        record = OrderRecord(
            order_id=order_id,
            broker_order_id=f"SIM_{order_id}",
            symbol=symbol,
            direction=direction,
            order_type=order_type,
            price=price,
            quantity=quantity,
            status=OrderStatus.FILLED,
            filled_quantity=quantity,
            average_fill_price=round(fill_price, 2),
            created_at=created_at,
            updated_at=created_at,
            tag=tag,
        )

        self.orders[order_id] = record

        # Update position tracker
        current_pos = self.positions.get(symbol, 0)
        pos_change = quantity if direction == OrderDirection.BUY else -quantity
        new_pos = current_pos + pos_change
        self.positions[symbol] = new_pos
        self.position_entry_prices[symbol] = fill_price

        logger.info(
            f"[{self.name}] FILLED {direction.value} {quantity} {symbol} @ ₹{fill_price:.2f} "
            f"(Slip: {self.slippage_points} pts) | Order ID: {order_id} | Net Position: {new_pos}"
        )
        return record

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self.orders:
            order = self.orders[order_id]
            if order.status in (OrderStatus.PENDING, OrderStatus.OPEN):
                order.status = OrderStatus.CANCELLED
                order.updated_at = datetime.now()
                logger.info(f"[{self.name}] Order {order_id} cancelled.")
                return True
        return False

    def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        cancelled = 0
        for o in self.orders.values():
            if symbol and o.symbol != symbol:
                continue
            if o.status in (OrderStatus.PENDING, OrderStatus.OPEN):
                o.status = OrderStatus.CANCELLED
                o.updated_at = datetime.now()
                cancelled += 1
        logger.info(f"[{self.name}] Cancelled {cancelled} pending orders.")
        return cancelled

    def get_positions(self) -> List[Dict[str, Any]]:
        result = []
        for sym, qty in self.positions.items():
            if qty != 0:
                result.append({
                    "symbol": sym,
                    "quantity": qty,
                    "buy_price": self.position_entry_prices.get(sym, 0.0),
                    "current_price": self.last_ltp.get(sym, 0.0),
                })
        return result

    def get_margins(self) -> Dict[str, float]:
        return {
            "net": self.capital,
            "available_cash": self.available_margin,
            "used_margin": self.capital - self.available_margin,
        }

    def start_market_stream(self, symbols: List[str], on_tick: Callable[[float, int, datetime], None]):
        self.tick_callback = on_tick
        logger.info(f"[{self.name}] Market stream callback registered for {symbols}.")
