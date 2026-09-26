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

    def place_order(self, order: OrderRecord) -> OrderRecord:
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError

    def get_positions(self) -> List[Dict[str, Any]]:
        return []

    def get_margins(self) -> Dict[str, Any]:
        return {}
