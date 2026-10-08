"""
In-memory real-time state repository for market instruments.
Maintains tick-by-tick prices, VWAP, Level-5 order book depth, and completed
15m candles received from the KiteTicker stream.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
import logging
import math
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

import pandas as pd

from backend.data.time_utils import now_ist_naive


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
        ).fillna(0)

        data = data.dropna(
            subset=[
                "datetime",
                "open",
                "high",
                "low",
                "close",
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

        return len(data)

    def update_tick(
        self,
        symbol: str,
        price: float,
        volume: int = 0,
        timestamp: Optional[datetime] = None,
        token: Optional[int] = None,
        day_open: Optional[float] = None,
        day_high: Optional[float] = None,
        day_low: Optional[float] = None,
        session_volume: Optional[int] = None,
    ) -> None:
        if timestamp is None:
            logger.warning(
                "[LiveMarketState] Rejecting tick with missing timestamp for %s",
                symbol,
            )
            return
        ts = timestamp

        try:
            num_price = float(price)
        except (TypeError, ValueError):
            return

        if not math.isfinite(num_price) or num_price <= 0:
            logger.warning(
                "[LiveMarketState] Rejecting non-finite/non-positive price for %s: %r",
                symbol,
                price,
            )
            return
        with self._lock:
            state = self._symbols.get(symbol)
            if state is None:
                state = LiveSymbolState(symbol=symbol, token=token)
                self._symbols[symbol] = state

            # Reject stale/out-of-order ticks before mutating state.
            if (
                state.last_tick_time is not None
                and ts < state.last_tick_time
            ):
                logger.warning(
                    "[LiveMarketState] Ignoring out-of-order "
                    "tick for %s: %s < %s",
                    symbol,
                    ts,
                    state.last_tick_time,
                )
                return

            # Detect session rollover defensively.
            if (
                state.last_tick_time is not None
                and state.last_tick_time.date() != ts.date()
            ):
                state.open = None
                state.high = None
                state.low = None
                state.volume = None
                state.vwap = None

            state.ltp = num_price
            state.close = num_price

            # Prefer authoritative Kite day OHLC.
            if day_open is not None:
                try:
                    num_open = float(day_open)
                    if math.isfinite(num_open) and num_open > 0:
                        state.open = num_open
                except (TypeError, ValueError):
                    pass

            if day_high is not None:
                try:
                    num_high = float(day_high)
                    if math.isfinite(num_high) and num_high > 0:
                        state.high = num_high
                except (TypeError, ValueError):
                    pass

            if day_low is not None:
                try:
                    num_low = float(day_low)
                    if math.isfinite(num_low) and num_low > 0:
                        state.low = num_low
                except (TypeError, ValueError):
                    pass

            # Prefer authoritative Kite session volume.
            if (
                session_volume is not None
                and session_volume >= 0
            ):
                state.volume = int(session_volume)
            elif volume > 0:
                state.volume = (
                    (state.volume or 0) + int(volume)
                )

            state.last_tick_time = ts
            state.updated_at = now_ist_naive()

    def add_volume(
        self,
        symbol: str,
        volume: int,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """
        Add an authoritative incremental traded-volume amount for `symbol`.

        This is the single integration point for real Kite session volume
        reaching live state. The caller (CandleAggregator, via
        MarketStreamManager) is responsible for converting Kite's
        cumulative `volume_traded` into a correct incremental amount so
        that volume is never double counted and never fabricated here.

        This is intentionally separate from update_tick(): update_tick()
        owns live LTP/OHLC and is called once per raw tick, while volume
        is only known correctly once a tick has gone through the
        cumulative-to-incremental conversion shared with candle
        aggregation. Keeping them separate means that conversion never
        has to be duplicated.
        """
        try:
            vol = int(volume)
        except (TypeError, ValueError):
            return

        if vol <= 0:
            return

        ts = timestamp or now_ist_naive()

        with self._lock:
            state = self._symbols.get(symbol)
            if state is None:
                state = LiveSymbolState(symbol=symbol)
                self._symbols[symbol] = state

            state.volume = (state.volume or 0) + vol
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
            if st is None:
                return None
            return copy.copy(st)

    def get_all_symbols_state(self) -> Dict[str, LiveSymbolState]:
        with self._lock:
            return {sym: copy.copy(st) for sym, st in self._symbols.items()}

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