"""
Paper Trading Simulated Broker Engine Adapter.
Tracks position state, order execution, and synthetic market fills for paper trading.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid

from broker.base_broker import BaseBrokerAdapter
from database.models import OrderDirection, OrderRecord, OrderStatus, OrderType


class PaperBrokerAdapter(BaseBrokerAdapter):
    def __init__(self, initial_capital: float = 1_000_000.0, slippage_points: float = 0.5):
        super().__init__(name="PAPER_BROKER")
        self.capital = initial_capital
        self.slippage_points = slippage_points
        self.positions: Dict[str, int] = {}
        self.position_entry_prices: Dict[str, float] = {}
        self.ltps: Dict[str, float] = {}
        self.orders: Dict[str, OrderRecord] = {}

    def connect(self) -> bool:
        self._connected = True
        return True

    def set_ltp(self, symbol: str, ltp: float):
        self.ltps[symbol] = ltp

    def place_order(self, order: OrderRecord) -> OrderRecord:
        order_id = order.order_id or f"paper_{uuid.uuid4().hex[:8]}"
        symbol = order.symbol
        qty = order.quantity
        direction = order.direction

        ltp = self.ltps.get(symbol, order.price or 1000.0)
        fill_price = ltp

        if direction in (OrderDirection.BUY, "BUY"):
            fill_price += self.slippage_points
            curr_qty = self.positions.get(symbol, 0)
            new_qty = curr_qty + qty
            self.positions[symbol] = new_qty
            self.position_entry_prices[symbol] = fill_price
        else:
            fill_price -= self.slippage_points
            curr_qty = self.positions.get(symbol, 0)
            new_qty = curr_qty - qty
            self.positions[symbol] = new_qty
            if new_qty == 0:
                self.position_entry_prices.pop(symbol, None)
            else:
                self.position_entry_prices[symbol] = fill_price

        order.order_id = order_id
        order.status = OrderStatus.FILLED
        order.average_price = fill_price
        order.filled_quantity = qty
        self.orders[order_id] = order
        return order

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self.orders:
            self.orders[order_id].status = OrderStatus.CANCELLED
            return True
        return False

    def get_positions(self) -> List[Dict[str, Any]]:
        res = []
        for symbol, qty in self.positions.items():
            avg = self.position_entry_prices.get(symbol, 0.0)
            ltp = self.ltps.get(symbol, avg)
            res.append({
                "tradingsymbol": symbol,
                "exchange": "NSE",
                "product": "MIS",
                "quantity": qty,
                "average_price": avg,
                "last_price": ltp,
                "realised": 0.0,
            })
        return res

    def get_margins(self) -> Dict[str, Any]:
        return {
            "equity": {
                "available": {
                    "cash": self.capital,
                    "live_balance": self.capital,
                },
                "utilised": {
                    "debits": 0.0,
                },
            }
        }
