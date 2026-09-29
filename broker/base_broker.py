"""
Abstract Base Broker Gateway Adapter Interface.
"""

from typing import Any, Dict, List, Optional
from database.models import OrderRecord


class BaseBrokerAdapter:
    def __init__(self, name: str = "BASE"):
        self.name = name
        self._connected = False

    def connect(self) -> bool:
        self._connected = True
        return True

    def disconnect(self):
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_positions(self) -> List[Dict[str, Any]]:
        return []

    def get_orders(self) -> List[Dict[str, Any]]:
        return []

    def get_margins(self) -> Dict[str, Any]:
        return {}

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
        """Places an order with the broker gateway. Returns the unique broker order_id."""
        raise NotImplementedError(f"[{self.name}] place_order not implemented.")

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
        """Modifies an existing open order. Returns order_id."""
        raise NotImplementedError(f"[{self.name}] modify_order not implemented.")

    def cancel_order(
        self,
        variety: str = "regular",
        order_id: str = "",
        parent_order_id: Optional[str] = None,
    ) -> str:
        """Cancels an existing open order. Returns order_id."""
        raise NotImplementedError(f"[{self.name}] cancel_order not implemented.")
