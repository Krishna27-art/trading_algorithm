"""
Base Strategy Interface & Signal Data Model.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, time, date
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
    signal_id: Optional[str] = None

    def __post_init__(self):
        if not self.signal_id:
            ts_str = self.timestamp.isoformat() if isinstance(self.timestamp, datetime) else str(self.timestamp)
            self.signal_id = f"SIG_{self.symbol}_{ts_str}_{self.action.value}"


class BaseStrategy(ABC):
    def __init__(self, symbol: str):
        self.symbol = symbol

        # Shared intraday position-state bookkeeping. Every strategy that
        # trades one position at a time (all of ours do — single instrument,
        # max 1 trade/day) needs the same fields; previously IntradayORBStrategy
        # duplicated this in its own __init__ instead of it living here.
        self.position: int = 0  # +1 Long, -1 Short, 0 Flat
        self.entry_price: float = 0.0
        self.stop_loss: float = 0.0
        self.target: float = 0.0
        self.initial_risk_dist: float = 0.0
        self.trailing_breakeven_active: bool = False
        self.trades_today: int = 0

    @abstractmethod
    def reset_session(self, session_date: date):
        """Called daily before market open. Should NOT clear any indicator
        state a strategy carries across days (e.g. a running EMA/ATR buffer)
        — only the per-session position/trade-count fields above."""
        pass

    @abstractmethod
    def on_candle(self, candle: dict, vwap: float) -> Optional[StrategySignal]:
        """Evaluates completed bar against strategy rules."""
        pass

    @abstractmethod
    def on_tick(self, price: float, timestamp: datetime) -> Optional[StrategySignal]:
        """Monitors intraday stop-loss, target, and trailing triggers."""
        pass

    def seed_context(self, historical_bars: pd.DataFrame) -> None:
        """
        Optional hook, called once per session BEFORE reset_session(), with a
        trailing window of prior sessions' bars (same OHLCV shape as
        on_candle's `candle` dict, concatenated across days). Strategies that
        need cross-day context — prior-day pivot levels, warm-up for a long
        moving average — override this. Default is a no-op; ORB doesn't need
        it since its opening range is computed fresh from the current day
        alone.
        """
        pass

    def register_trade_entry(self, entry_price: float, position: int, stop_loss: float, target: float, risk_dist: float):
        """Default fill-confirmation hook shared by every single-position
        strategy: records the position the backtester/execution engine just
        opened. Override only if a strategy needs extra bookkeeping."""
        self.position = position
        self.entry_price = entry_price
        self.stop_loss = stop_loss
        self.target = target
        self.initial_risk_dist = risk_dist
        self.trailing_breakeven_active = False
        self.trades_today += 1

    def register_trade_exit(self):
        """Default exit-confirmation hook — clears the open-position state."""
        self.position = 0
        self.entry_price = 0.0
        self.stop_loss = 0.0
        self.target = 0.0
        self.initial_risk_dist = 0.0
        self.trailing_breakeven_active = False
