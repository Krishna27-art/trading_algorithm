"""
Base Strategy Interface & Signal Data Model.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
from typing import Any, Dict, Optional
import pandas as pd


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    EXIT = "EXIT"
    HOLD = "HOLD"


@dataclass
class StrategySignal:
    action: SignalAction
    symbol: str
    timestamp: datetime
    price: float
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    reason: str = ""
    order_type: str = "LIMIT"
    product: str = "MIS"


class BaseStrategy(ABC):
    def __init__(self, symbol: str):
        self.symbol = symbol

    @abstractmethod
    def reset_session(self, session_date: datetime.date):
        """Called daily before market open."""
        pass

    @abstractmethod
    def on_candle(self, candle: dict, vwap: float) -> Optional[StrategySignal]:
        """Evaluates completed bar against strategy rules."""
        pass

    @abstractmethod
    def on_tick(self, price: float, timestamp: datetime) -> Optional[StrategySignal]:
        """Monitors intraday stop-loss, target, and trailing triggers."""
        pass
