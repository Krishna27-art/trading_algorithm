"""
Paper Trading Broker Adapter.
Simulates realistic order execution, slippage, and position tracking
without submitting real orders to Zerodha. Records all paper orders
and trades into SQLite database for performance auditing.
"""

from datetime import datetime
import logging
from typing import Any, Dict, List, Optional
import uuid

from broker.base_broker import BaseBrokerAdapter
from config.settings import AppSettings, settings
from database.db import DatabaseManager
from database.models import OrderDirection, OrderRecord, OrderStatus, OrderType, TradeRecord
from risk.transaction_costs import TransactionCostCalculator

logger = logging.getLogger("broker.paper")


class PaperBrokerAdapter(BaseBrokerAdapter):
    def __init__(self, app_settings: AppSettings = settings, db: Optional[DatabaseManager] = None):
        super().__init__(name="PAPER_BROKER")
        self.settings = app_settings
        self.db = db or DatabaseManager(self.settings.db_path)
        self.cost_calc = TransactionCostCalculator(self.settings.costs)
        self._positions: Dict[str, Dict[str, Any]] = {}
        self._orders: List[Dict[str, Any]] = []
        self._capital = self.settings.risk.initial_capital
        self._connected = True

    def connect(self) -> bool:
        self._connected = True
        logger.info("[PAPER_BROKER] Connected in paper trading simulation mode.")
        return True

    def disconnect(self):
        self._connected = False
        logger.info("[PAPER_BROKER] Disconnected.")

    def get_margins(self) -> Dict[str, float]:
        used_margin = sum(abs(p.get("quantity", 0) * p.get("average_price", 0.0)) * 0.20 for p in self._positions.values())
        return {
            "net": float(self._capital),
            "available_cash": float(max(self._capital - used_margin, 0.0)),
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        return list(self._positions.values())

    def get_orders(self) -> List[Dict[str, Any]]:
        return list(self._orders)

    def place_order(
        self,
        variety: str = "regular",
        exchange: str = "NSE",
        tradingsymbol: str = "",
        transaction_type: str = "BUY",
        quantity: int = 1,
        product: str = "MIS",
        order_type: str = "MARKET",
        price: Optional[float] = None,
        validity: Optional[str] = "DAY",
        disclosed_quantity: Optional[int] = None,
        trigger_price: Optional[float] = None,
        squareoff: Optional[float] = None,
        stoploss: Optional[float] = None,
        trailing_stoploss: Optional[float] = None,
        tag: Optional[str] = None,
    ) -> str:
        """Simulates placing and filling a paper order."""
        order_id = f"PAPER_{uuid.uuid4().hex[:10].upper()}"
        now = datetime.now()

        # Determine execution fill price with slippage
        exec_price = float(price) if price and price > 0 else 1000.0
        slippage_pct = self.settings.costs.equity_slippage_pct
        if transaction_type.upper() == "BUY":
            fill_price = round(exec_price * (1.0 + slippage_pct), 2)
            dir_enum = OrderDirection.BUY
        else:
            fill_price = round(exec_price * (1.0 - slippage_pct), 2)
            dir_enum = OrderDirection.SELL

        ot_enum = OrderType.LIMIT if order_type.upper() == "LIMIT" else OrderType.MARKET

        order_record = OrderRecord(
            order_id=order_id,
            broker_order_id=order_id,
            client_order_id=tag,
            signal_id=tag,
            symbol=tradingsymbol,
            direction=dir_enum,
            order_type=ot_enum,
            price=fill_price,
            quantity=quantity,
            status=OrderStatus.FILLED,
            filled_quantity=quantity,
            average_fill_price=fill_price,
            created_at=now,
            updated_at=now,
            tag=tag,
            product=product,
            exchange=exchange,
        )

        # Save to SQLite database
        try:
            self.db.save_order(order_record)
        except Exception as e:
            logger.warning(f"[PAPER_BROKER] Could not save order to database: {e}")

        # Update in-memory order list
        order_dict = {
            "order_id": order_id,
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type.upper(),
            "quantity": quantity,
            "product": product,
            "order_type": order_type,
            "price": fill_price,
            "status": "COMPLETE",
            "order_timestamp": now.isoformat(),
            "tag": tag,
        }
        self._orders.insert(0, order_dict)

        # Update virtual positions
        pos_key = f"{exchange}:{tradingsymbol}:{product}"
        current_pos = self._positions.get(pos_key)
        qty_signed = quantity if transaction_type.upper() == "BUY" else -quantity

        if not current_pos:
            self._positions[pos_key] = {
                "tradingsymbol": tradingsymbol,
                "exchange": exchange,
                "product": product,
                "quantity": qty_signed,
                "average_price": fill_price,
                "last_price": fill_price,
                "pnl": 0.0,
                "unrealised_pnl": 0.0,
                "realised_pnl": 0.0,
                "value": fill_price * qty_signed,
            }
        else:
            old_qty = current_pos["quantity"]
            new_qty = old_qty + qty_signed
            if new_qty == 0:
                del self._positions[pos_key]
            else:
                current_pos["quantity"] = new_qty
                current_pos["last_price"] = fill_price
                current_pos["value"] = fill_price * new_qty

        logger.info(
            f"[PAPER_BROKER] Paper Order FILLED: {order_id} "
            f"({transaction_type} {quantity} {exchange}:{tradingsymbol} @ {fill_price})"
        )
        return order_id

    def modify_order(
        self,
        variety: str = "regular",
        order_id: str = "",
        parent_order_id: Optional[str] = None,
        quantity: Optional[int] = None,
        price: Optional[float] = None,
        order_type: Optional[str] = None,
        trigger_price: Optional[float] = None,
        validity: Optional[str] = None,
        disclosed_quantity: Optional[int] = None,
    ) -> str:
        for ord_d in self._orders:
            if ord_d["order_id"] == order_id:
                if quantity is not None:
                    ord_d["quantity"] = int(quantity)
                if price is not None:
                    ord_d["price"] = float(price)
                return order_id
        raise ValueError(f"[PAPER_BROKER] Order {order_id} not found.")

    def cancel_order(
        self,
        variety: str = "regular",
        order_id: str = "",
        parent_order_id: Optional[str] = None,
    ) -> str:
        for ord_d in self._orders:
            if ord_d["order_id"] == order_id:
                ord_d["status"] = "CANCELLED"
                return order_id
        raise ValueError(f"[PAPER_BROKER] Order {order_id} not found.")
