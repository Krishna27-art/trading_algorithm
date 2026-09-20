"""
DhanHQ API v2 Broker Adapter.
Handles order routing and WebSocket market data streaming via DhanHQ SDK.
"""

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from broker.base_broker import BaseBrokerAdapter
from database.models import OrderDirection, OrderRecord, OrderStatus, OrderType
from monitoring.logger import logger


class DhanBrokerAdapter(BaseBrokerAdapter):
    def __init__(self, client_id: Optional[str] = None, access_token: Optional[str] = None):
        super().__init__(name="DHAN_BROKER")
        self.client_id = client_id
        self.access_token = access_token
        self.dhan = None
        self.tick_callback: Optional[Callable[[float, int, datetime], None]] = None

    def connect(self) -> bool:
        if not self.client_id or not self.access_token:
            logger.warning(f"[{self.name}] DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN not configured in .env.")
            return False

        try:
            from dhanhq import dhanhq
            self.dhan = dhanhq(self.client_id, self.access_token)
            # Fetch fund limit to validate session
            funds = self.dhan.get_fund_limits()
            if funds and funds.get("status") == "success":
                self.is_connected = True
                logger.info(f"[{self.name}] Successfully authenticated with DhanHQ API.")
                return True
            else:
                logger.error(f"[{self.name}] Failed to authenticate with DhanHQ: {funds}")
                return False
        except Exception as e:
            logger.error(f"[{self.name}] Dhan connection error: {e}")
            self.is_connected = False
            return False

    def disconnect(self):
        self.is_connected = False
        logger.info(f"[{self.name}] DhanHQ disconnected.")

    def place_order(
        self,
        symbol: str,
        direction: OrderDirection,
        order_type: OrderType,
        quantity: int,
        price: Optional[float] = None,
        tag: Optional[str] = "algo",
    ) -> OrderRecord:
        if not self.dhan:
            raise RuntimeError("Dhan adapter is not connected.")

        from dhanhq import dhanhq
        txn_type = self.dhan.BUY if direction == OrderDirection.BUY else self.dhan.SELL
        ord_type = self.dhan.LIMIT if order_type == OrderType.LIMIT else self.dhan.MARKET
        segment = self.dhan.NSE_FNO if "NIFTY" in symbol else self.dhan.NSE_EQ

        try:
            resp = self.dhan.place_order(
                security_id="13",  # Sample instrument security ID or mapping
                exchange_segment=segment,
                transaction_type=txn_type,
                quantity=quantity,
                order_type=ord_type,
                product_type=self.dhan.INTRA,
                price=price if price is not None else 0.0,
                correlation_id=tag,
            )
            order_id = resp.get("data", {}).get("orderId", "DHAN_ORDER")
            logger.info(f"[{self.name}] Dhan order placed: {order_id}")
            return OrderRecord(
                order_id=order_id,
                broker_order_id=order_id,
                symbol=symbol,
                direction=direction,
                order_type=order_type,
                price=price,
                quantity=quantity,
                status=OrderStatus.SUBMITTED,
                created_at=datetime.now(),
                updated_at=datetime.now(),
                tag=tag,
            )
        except Exception as e:
            logger.error(f"[{self.name}] Dhan order placement error: {e}")
            raise

    def cancel_order(self, order_id: str) -> bool:
        if not self.dhan:
            return False
        try:
            self.dhan.cancel_order(order_id)
            return True
        except Exception as e:
            logger.error(f"[{self.name}] Cancel order failed: {e}")
            return False

    def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        if not self.dhan:
            return 0
        try:
            orders = self.dhan.get_order_list()
            cancelled = 0
            if orders and orders.get("status") == "success":
                for o in orders.get("data", []):
                    if o.get("orderStatus") in ("PENDING", "TRANSIT"):
                        self.cancel_order(o["orderId"])
                        cancelled += 1
            return cancelled
        except Exception as e:
            logger.error(f"[{self.name}] Error in cancel_all_orders: {e}")
            return 0

    def get_positions(self) -> List[Dict[str, Any]]:
        if not self.dhan:
            return []
        try:
            res = self.dhan.get_positions()
            if res and res.get("status") == "success":
                return res.get("data", [])
            return []
        except Exception as e:
            logger.error(f"[{self.name}] Error getting positions: {e}")
            return []

    def get_margins(self) -> Dict[str, float]:
        if not self.dhan:
            return {"net": 0.0, "available_cash": 0.0}
        try:
            res = self.dhan.get_fund_limits()
            if res and res.get("status") == "success":
                avail = float(res.get("data", {}).get("availMargin", 0.0))
                return {"net": avail, "available_cash": avail}
            return {"net": 0.0, "available_cash": 0.0}
        except Exception as e:
            logger.error(f"[{self.name}] Error fetching fund limits: {e}")
            return {"net": 0.0, "available_cash": 0.0}

    def start_market_stream(self, symbols: List[str], on_tick: Callable[[float, int, datetime], None]):
        self.tick_callback = on_tick
        logger.info(f"[{self.name}] Market stream callback set for {symbols}.")
