"""
In-memory real-time state repository for market instruments.
Maintains tick-by-tick prices, VWAP, Level-5 order book depth, and completed
15m candles received from the KiteTicker stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
import threading
from typing import Any, Dict, List, Optional

import pandas as pd

from data.time_utils import now_ist_naive

logger = logging.getLogger(__name__)


@dataclass
class LiveSymbolState:
    symbol: str
    token: Optional[int] = None
    ltp: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[int] = None
    vwap: Optional[float] = None
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

    def __init__(self, max_candle_history: int = 640):
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

    def seed_historical_candles(
        self,
        symbol: str,
        candles: pd.DataFrame,
    ) -> int:
        """
        Seed completed historical 15-minute candles into live state.

        Historical candles are used only for strategy context.
        They NEVER overwrite the current live LTP.

        Returns:
            Number of candles successfully seeded.
        """
        if (
            candles is None
            or not isinstance(candles, pd.DataFrame)
            or candles.empty
        ):
            return 0

        required = {
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume",
        }

        if not required.issubset(candles.columns):
            return 0

        data = candles.copy()

        data["datetime"] = pd.to_datetime(
            data["datetime"],
            errors="coerce",
        )

        data["open"] = pd.to_numeric(
            data["open"],
            errors="coerce",
        )

        data["high"] = pd.to_numeric(
            data["high"],
            errors="coerce",
        )

        data["low"] = pd.to_numeric(
            data["low"],
            errors="coerce",
        )

        data["close"] = pd.to_numeric(
            data["close"],
            errors="coerce",
        )

        data["volume"] = pd.to_numeric(
            data["volume"],
            errors="coerce",
        )

        data = data.dropna(
            subset=[
                "datetime",
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]
        ).copy()

        if data.empty:
            return 0

        # Never seed future data.
        now = now_ist_naive()

        data = data[
            data["datetime"] <= now
        ].copy()

        if data.empty:
            return 0

        data = data[
            (data["open"] > 0)
            & (data["high"] > 0)
            & (data["low"] > 0)
            & (data["close"] > 0)
            & (data["high"] >= data["low"])
            & (data["high"] >= data["open"])
            & (data["high"] >= data["close"])
            & (data["low"] <= data["open"])
            & (data["low"] <= data["close"])
            & (data["volume"] >= 0)
        ].copy()

        data = (
            data.sort_values("datetime")
            .drop_duplicates(
                subset=["datetime"],
                keep="last",
            )
            .tail(self._max_candle_history)
            .reset_index(drop=True)
        )

        if data.empty:
            return 0

        normalized_symbol = str(
            symbol
        ).strip().upper()

        with self._lock:
            state = self._symbols.get(
                normalized_symbol
            )

            if state is None:
                state = LiveSymbolState(
                    symbol=normalized_symbol
                )
                self._symbols[
                    normalized_symbol
                ] = state

            existing = pd.DataFrame(
                state.completed_candles
            )

            if (
                not existing.empty
                and "datetime" in existing.columns
            ):
                existing["datetime"] = pd.to_datetime(
                    existing["datetime"],
                    errors="coerce",
                )

                merged = pd.concat(
                    [
                        existing,
                        data,
                    ],
                    ignore_index=True,
                )
            else:
                merged = data.copy()

            merged = (
                merged
                .dropna(subset=["datetime"])
                .sort_values("datetime")
                .drop_duplicates(
                    subset=["datetime"],
                    keep="last",
                )
                .tail(self._max_candle_history)
                .reset_index(drop=True)
            )

            state.completed_candles = (
                merged.to_dict("records")
            )

            if state.completed_candles:
                state.latest_completed_candle = dict(
                    state.completed_candles[-1]
                )

            # Seed the real 09:15 session open from the first candle
            # of today in the historical data. This ensures the live
            # open is always the real NSE session open, not the price
            # of the first tick seen after the stream started.
            if state.open is None:
                today_date = now_ist_naive().date()
                for row in state.completed_candles:
                    dt = row.get("datetime")
                    try:
                        dt_val = pd.Timestamp(dt)
                        if dt_val.date() == today_date:
                            open_val = float(row.get("open", 0) or 0)
                            if open_val > 0:
                                state.open = open_val
                            break
                    except Exception:
                        continue

        return len(data)

    def update_tick(
        self,
        symbol: str,
        price: float,
        volume: Optional[int] = None,
        timestamp: Optional[datetime] = None,
        token: Optional[int] = None,
    ) -> None:
        if timestamp is None:
            logger.warning("[LiveMarketState] Rejecting tick without exchange timestamp.")
            return
        ts = timestamp
        with self._lock:
            state = self._symbols.get(symbol)
            if state is None:
                state = LiveSymbolState(symbol=symbol, token=token)
                self._symbols[symbol] = state

            state.ltp = price
            state.close = price
            # Only set open on first tick if it was not already seeded
            # from the historical 09:15 candle by seed_historical_candles().
            if state.open is None:
                state.open = price
                state.high = price
                state.low = price
            else:
                state.high = max(state.high, price) if state.high is not None else price
                state.low = min(state.low, price) if state.low is not None else price

            # `volume` is Kite's cumulative session volume (volume_traded).
            # Set it directly — do NOT accumulate it as if it were incremental.
            if volume is not None and volume >= 0:
                state.volume = int(volume)

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

            state.vwap = float(vwap) if (vwap is not None and vwap > 0) else None

            # NEVER overwrite live LTP/close with a completed candle close.
            # update_tick() is the only live-price writer.

            state.latest_completed_candle = dict(
                candle_dict
            )

            # Store in rolling buffer, avoiding duplicates by datetime.
            dt = candle_dict.get(
                "datetime"
            )

            if not any(
                c.get("datetime") == dt
                for c in state.completed_candles
            ):
                state.completed_candles.append(
                    dict(candle_dict)
                )

                if (
                    len(state.completed_candles)
                    > self._max_candle_history
                ):
                    state.completed_candles.pop(0)

            # IMPORTANT:
            # Do NOT update state.last_tick_time.
            # Do NOT update state.updated_at here.
            #
            # Those fields describe live tick freshness, not candle completion.

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
