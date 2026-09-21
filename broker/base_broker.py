"""
Abstract Broker Adapter Interface for multi-broker abstraction.
Supports KiteConnect v3, DhanHQ v2, and Paper Trading Simulator.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from database.models import OrderDirection, OrderRecord, OrderStatus, OrderType


class BaseBrokerAdapter(ABC):
    def __init__(self, name: str):
        self.name = name
        self.is_connected: bool = False

    @abstractmethod
    def connect(self) -> bool:
        """Authenticates session and connects to broker."""
        pass

    @abstractmethod
    def disconnect(self):
        """Cleanly disconnects session and WebSocket streams."""
        pass

    @abstractmethod
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
        """Dispatches an order to the broker and returns tracking record."""
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancels an active resting order."""
        pass

    @abstractmethod
    def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """Cancels all pending orders (used during 14:30 square-off or kill-switch)."""
        pass

    @abstractmethod
    def get_positions(self) -> List[Dict[str, Any]]:
        """Fetches active day positions."""
        pass

    @abstractmethod
    def get_orders(self) -> List[Dict[str, Any]]:
        """Fetches all orders placed with broker today."""
        pass

    @abstractmethod
    def get_margins(self) -> Dict[str, float]:
        """Fetches available capital and cash margins."""
        pass

    @abstractmethod
    def start_market_stream(self, symbols: List[str], on_tick: Callable[[float, int, datetime], None]):
        """Starts WebSocket market stream with automatic reconnect."""
        pass
