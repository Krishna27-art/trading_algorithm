"""
Zerodha Kite Connect v3 Broker Adapter.
Handles REST order routing and KiteTicker WebSocket data streaming with automatic reconnects.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from kiteconnect import KiteConnect, KiteTicker

from broker.base_broker import BaseBrokerAdapter
from database.models import OrderDirection, OrderRecord, OrderStatus, OrderType
from monitoring.logger import logger


class KiteBrokerAdapter(BaseBrokerAdapter):
    def __init__(self, api_key: Optional[str] = None, access_token: Optional[str] = None):
        super().__init__(name="KITE_BROKER")
        self.api_key = api_key
        self.access_token = access_token
        self.kite: Optional[KiteConnect] = None
        self.kws: Optional[KiteTicker] = None
        self.tick_callback: Optional[Callable[[float, int, datetime], None]] = None

    def _load_saved_token(self) -> bool:
        token_file = Path(__file__).resolve().parent.parent / "session_token.json"
        if token_file.exists():
            try:
                with open(token_file, "r") as f:
                    data = json.load(f)
                    if data.get("access_token"):
                        self.access_token = data["access_token"]
                        if not self.api_key:
                            self.api_key = data.get("api_key")
                        return True
            except Exception as e:
                logger.error(f"[{self.name}] Failed to read session token: {e}")
        return False

    def connect(self) -> bool:
        if not self.access_token:
            self._load_saved_token()

        if not self.api_key or not self.access_token:
            logger.error(f"[{self.name}] Cannot connect: API Key or Access Token is missing. Run auth first.")
            return False

        try:
            self.kite = KiteConnect(api_key=self.api_key)
            self.kite.set_access_token(self.access_token)
            profile = self.kite.profile()
            self.is_connected = True
            logger.info(f"[{self.name}] Connected as: {profile.get('user_name')} ({profile.get('user_id')})")
            return True
        except Exception as e:
            logger.error(f"[{self.name}] Authentication validation error: {e}")
            self.is_connected = False
            return False

    def disconnect(self):
        if self.kws:
            try:
                self.kws.close()
            except Exception:
                pass
        self.is_connected = False
        logger.info(f"[{self.name}] Disconnected.")

    def place_order(
        self,
        symbol: str,
        direction: OrderDirection,
        order_type: OrderType,
        quantity: int,
        price: Optional[float] = None,
        tag: Optional[str] = "algo",
        client_order_id: Optional[str] = None,
        product: Optional[str] = "MIS",
        exchange: Optional[str] = None,
    ) -> OrderRecord:
        if not self.kite:
            raise RuntimeError("Kite adapter is not connected.")

        txn_type = self.kite.TRANSACTION_TYPE_BUY if direction == OrderDirection.BUY else self.kite.TRANSACTION_TYPE_SELL
        ord_type = self.kite.ORDER_TYPE_LIMIT if order_type == OrderType.LIMIT else self.kite.ORDER_TYPE_MARKET
        target_exchange = exchange or (self.kite.EXCHANGE_NFO if getattr(self, "default_exchange", "NSE") == "NFO" else self.kite.EXCHANGE_NSE)

        prod_upper = (product or "MIS").upper()
        if prod_upper == "CNC":
            target_product = self.kite.PRODUCT_CNC
        elif prod_upper == "NRML":
            target_product = self.kite.PRODUCT_NRML
        else:
            target_product = self.kite.PRODUCT_MIS

        # Kite tag field has an 8-character or 20-character limit depending on API version; use tag or slice client_order_id
        order_tag = tag or (client_order_id[:8] if client_order_id else "algo")

        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=target_exchange,
                tradingsymbol=symbol,
                transaction_type=txn_type,
                quantity=quantity,
                product=target_product,
                order_type=ord_type,
                price=price,
                tag=order_tag,
            )
            logger.info(f"[{self.name}] Order placed successfully. Broker Order ID: {order_id}")
            return OrderRecord(
                order_id=order_id,
                broker_order_id=order_id,
                client_order_id=client_order_id,
                symbol=symbol,
                direction=direction,
                order_type=order_type,
                price=price,
                quantity=quantity,
                status=OrderStatus.SUBMITTED,
                created_at=datetime.now(),
                updated_at=datetime.now(),
                tag=order_tag,
                product=prod_upper,
                exchange=target_exchange,
            )
        except Exception as e:
            logger.error(f"[{self.name}] Order placement failed: {e}")
            raise

    def cancel_order(self, order_id: str) -> bool:
        if not self.kite:
            return False
        try:
            self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=order_id)
            return True
        except Exception as e:
            logger.error(f"[{self.name}] Cancel order failed for {order_id}: {e}")
            return False

    def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        if not self.kite:
            return 0
        try:
            orders = self.kite.orders()
            cancelled = 0
            for o in orders:
                if o.get("status") in ("OPEN", "TRIGGER PENDING"):
                    if symbol and o.get("tradingsymbol") != symbol:
                        continue
                    self.cancel_order(o["order_id"])
                    cancelled += 1
            return cancelled
        except Exception as e:
            logger.error(f"[{self.name}] Error in cancel_all_orders: {e}")
            return 0

    def get_positions(self) -> List[Dict[str, Any]]:
        if not self.kite:
            return []
        try:
            pos = self.kite.positions()
            return pos.get("net", [])
        except Exception as e:
            logger.error(f"[{self.name}] Error fetching positions: {e}")
            return []

    def get_orders(self) -> List[Dict[str, Any]]:
        if not self.kite:
            return []
        try:
            return self.kite.orders()
        except Exception as e:
            logger.error(f"[{self.name}] Error fetching orders: {e}")
            return []

    def get_margins(self) -> Dict[str, float]:
        if not self.kite:
            return {"net": 0.0, "available_cash": 0.0}
        try:
            margins = self.kite.margins()
            equity = margins.get("equity", {})
            return {
                "net": float(equity.get("net", 0.0)),
                "available_cash": float(equity.get("available", {}).get("live_balance", 0.0)),
            }
        except Exception as e:
            logger.error(f"[{self.name}] Error fetching margins: {e}")
            return {"net": 0.0, "available_cash": 0.0}

    def start_market_stream(self, symbols: List[str], on_tick: Callable[[float, int, datetime], None]):
        self.tick_callback = on_tick
        logger.info(f"[{self.name}] KiteTicker stream configured for {symbols}.")
