"""
In-memory real-time state repository for market instruments.
Maintains tick-by-tick prices, VWAP, Level-5 order book depth, and completed
15m candles received from the KiteTicker stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import threading
from typing import Any, Dict, List, Optional

import pandas as pd

from data.time_utils import now_ist_naive


@dataclass
class LiveSymbolState:
    symbol: str
    token: Optional[int] = None
    ltp: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    vwap: float = 0.0
    last_tick_time: Optional[datetime] = None
    latest_completed_candle: Optional[Dict[str, Any]] = None
    completed_candles: List[Dict[str, Any]] = field(default_factory=list)
    book_snapshot: Optional[Any] = None
    data_source: str = "STREAM"
    updated_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "token": self.token,
            "ltp": self.ltp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "vwap": self.vwap,
            "last_tick_time": self.last_tick_time.isoformat() if self.last_tick_time else None,
            "latest_completed_candle": self.latest_completed_candle,
            "completed_candles_count": len(self.completed_candles),
            "has_book_snapshot": self.book_snapshot is not None,
            "data_source": self.data_source,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class LiveMarketState:
    """Thread-safe store of live streaming market state for all subscribed symbols."""

    def __init__(self, max_candle_history: int = 100):
        self._lock = threading.Lock()
        self._symbols: Dict[str, LiveSymbolState] = {}
        self._token_to_symbol: Dict[int, str] = {}
        self._max_candle_history = max_candle_history

    def set_token_map(self, token_to_symbol: Dict[int, str]) -> None:
        with self._lock:
            self._token_to_symbol = dict(token_to_symbol)
            for token, symbol in token_to_symbol.items():
                if symbol not in self._symbols:
                    self._symbols[symbol] = LiveSymbolState(symbol=symbol, token=token)
                else:
                    self._symbols[symbol].token = token

    def update_tick(
        self,
        symbol: str,
        price: float,
        volume: int,
        timestamp: Optional[datetime] = None,
        token: Optional[int] = None,
    ) -> None:
        ts = timestamp or now_ist_naive()
        with self._lock:
            state = self._symbols.get(symbol)
            if state is None:
                state = LiveSymbolState(symbol=symbol, token=token)
                self._symbols[symbol] = state

            state.ltp = price
            state.close = price
            if state.open == 0.0:
                state.open = price
                state.high = price
                state.low = price
            else:
                state.high = max(state.high, price)
                state.low = min(state.low, price)
            state.volume += volume
            state.last_tick_time = ts
            state.updated_at = ts

    def update_candle_close(self, candle_dict: Dict[str, Any], vwap: float) -> None:
        symbol = candle_dict.get("symbol")
        if not symbol:
            return

        with self._lock:
            state = self._symbols.get(symbol)
            if state is None:
                state = LiveSymbolState(symbol=symbol)
                self._symbols[symbol] = state

            state.vwap = vwap
            state.ltp = float(candle_dict.get("close", state.ltp))
            state.close = state.ltp
            state.latest_completed_candle = dict(candle_dict)

            # Store in rolling buffer, avoiding duplicates by datetime
            dt = candle_dict.get("datetime")
            if not any(c.get("datetime") == dt for c in state.completed_candles):
                state.completed_candles.append(dict(candle_dict))
                if len(state.completed_candles) > self._max_candle_history:
                    state.completed_candles.pop(0)

            state.updated_at = now_ist_naive()

    def update_book_snapshot(self, symbol: str, snapshot: Any) -> None:
        with self._lock:
            state = self._symbols.get(symbol)
            if state is None:
                state = LiveSymbolState(symbol=symbol)
                self._symbols[symbol] = state
            state.book_snapshot = snapshot
            state.updated_at = now_ist_naive()

    def get_symbol_state(self, symbol: str) -> Optional[LiveSymbolState]:
        with self._lock:
            st = self._symbols.get(symbol)
            return st

    def get_all_symbols_state(self) -> Dict[str, LiveSymbolState]:
        with self._lock:
            return dict(self._symbols)

    def get_candles_df(self, symbol: str) -> pd.DataFrame:
        with self._lock:
            state = self._symbols.get(symbol)
            if not state or not state.completed_candles:
                return pd.DataFrame()
            return pd.DataFrame(state.completed_candles)

    def reset(self) -> None:
        with self._lock:
            self._symbols.clear()
            self._token_to_symbol.clear()


live_market_state = LiveMarketState()
