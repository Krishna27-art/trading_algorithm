"""
SSF Live Runtime — persistent per-symbol SsfL5SrmStrategy registry.

SsfL5SrmStrategy maintains extensive rolling state:
    _z_mlofi, _z_micro, _z_oi, _z_sector   — rolling z-score buffers
    _basis_hist                              — 30-min basis history
    _mids                                   — mid-price deque for vol est.
    _bars, _parkinson_hist                  — regime detector history
    pending                                 — passive order state (backtest only)
    last_features                           — output of the last _features()

If _evaluate_ssf_l5_srm() recreates SsfL5SrmStrategy() on every candle-
close, ALL of the above reset to zero/empty, making the z-score warm-up
period (z_warmup=60 observations by default) unreachable in practice.

This module owns one SsfL5SrmStrategy per symbol for the lifetime of the
stream session. It is instantiated lazily on first use and cleared when
the stream stops.

All live strategies are created with signal_only=True so they never create
fake pending orders for a manual decision-support system.
"""

from __future__ import annotations

import threading
from datetime import date
from typing import Any, Dict, Optional

from config.settings import settings
from config.universe import create_instrument_config_for_equity
from monitoring.logger import logger
from strategy.ssf_l5_srm_strategy import SsfL5SrmStrategy


class SSFLiveRuntime:
    """
    Thread-safe registry of persistent SsfL5SrmStrategy instances.

    One instance per subscribed symbol survives across every candle close and
    book-update event for the duration of the stream session.

    Lifecycle:
        - The owning component (LiveSignalEngine) creates one SSFLiveRuntime
          at startup and holds it for the session.
        - reset() must be called when the stream stops so that stale state
          does not persist into the next session.
    """

    def __init__(self, strategy_config: Optional[Any] = None) -> None:
        self._lock = threading.Lock()
        self._strategy_config = strategy_config or settings.strategy
        self._strategies: Dict[str, SsfL5SrmStrategy] = {}

    def get_strategy(
        self,
        symbol: str,
        token: int,
        current_price: float,
    ) -> SsfL5SrmStrategy:
        """
        Return the persistent SsfL5SrmStrategy for *symbol*, creating it once.

        The instrument config is built from the real market price supplied
        at first creation. Subsequent calls return the same instance so that
        all rolling buffers remain intact.

        All live instances are created with signal_only=True.
        """
        clean = str(symbol).strip().upper()

        with self._lock:
            existing = self._strategies.get(clean)
            if existing is not None:
                return existing

        # Build instrument config outside the lock (potentially expensive,
        # but create_instrument_config_for_equity is safe to call concurrently).
        instrument = create_instrument_config_for_equity(
            clean,
            int(token),
            current_price=float(current_price),
        )

        strategy = SsfL5SrmStrategy(
            instrument,
            self._strategy_config,
            signal_only=True,
        )

        with self._lock:
            # Double-check: another thread may have inserted one while we
            # were constructing ours.
            if clean not in self._strategies:
                self._strategies[clean] = strategy
                logger.debug(
                    "[SSFLiveRuntime] Created persistent signal-only strategy for %s",
                    clean,
                )
            return self._strategies[clean]

    def seed_regime_history(
        self,
        symbol: str,
        token: int,
        candles,
    ) -> int:
        """
        Warm the persistent SSF regime detector with real historical 1-minute bars.

        Returns the number of candles fed into the strategy.
        """
        if candles is None or candles.empty:
            return 0
        clean = str(symbol).strip().upper()
        first_close = float(candles["close"].iloc[-1])
        strategy = self.get_strategy(
            symbol=clean,
            token=int(token),
            current_price=first_close,
        )
        count = 0
        for _, row in candles.iterrows():
            try:
                candle = {
                    "datetime": row["datetime"],
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": max(int(row.get("volume", 0)), 0),
                }
                strategy.on_candle(candle, 0.0)
                count += 1
            except (TypeError, ValueError, KeyError):
                continue
        return count

    def on_one_minute_candle(
        self,
        symbol: str,
        token: int,
        candle: dict,
        vwap: float = 0.0,
    ) -> None:
        """
        Feed a completed real 1-minute candle into the persistent SSF regime.
        """
        current_price = float(candle["close"])
        strategy = self.get_strategy(
            symbol=symbol,
            token=int(token),
            current_price=current_price,
        )
        strategy.on_candle(candle, vwap)

    def is_regime_ready(
        self,
        symbol: str,
        min_bars: int = 30,
    ) -> bool:
        clean = str(symbol).strip().upper()
        with self._lock:
            strategy = self._strategies.get(clean)
            if strategy is None:
                return False
            return len(strategy._bars) >= int(min_bars)

    def prepare_session(self, session_date: date) -> None:
        """
        Reset only intraday execution state. Regime history remains intact.
        """
        with self._lock:
            strategies = list(self._strategies.values())
        for strategy in strategies:
            strategy.reset_session(session_date)

    def reset_symbol(self, symbol: str) -> None:
        """Remove the strategy for *symbol*, e.g. after a session roll-over."""
        clean = str(symbol).strip().upper()
        with self._lock:
            removed = self._strategies.pop(clean, None)
        if removed is not None:
            logger.debug(
                "[SSFLiveRuntime] Cleared persistent strategy for %s",
                clean,
            )

    def reset(self) -> None:
        """Clear all persistent strategies (call on stream stop)."""
        with self._lock:
            count = len(self._strategies)
            self._strategies.clear()
        logger.info(
            "[SSFLiveRuntime] Cleared %d persistent SSF strategies.",
            count,
        )


# Global singleton instance for live SSF strategy state
ssf_live_runtime = SSFLiveRuntime()

