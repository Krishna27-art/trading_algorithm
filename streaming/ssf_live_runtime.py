"""
SSF Live Runtime — persistent per-symbol SsfL5SrmStrategy registry.

SsfL5SrmStrategy maintains extensive rolling state:
    _z_mlofi, _z_micro, _z_oi, _z_sector   — rolling z-score buffers
    _basis_hist                              — 30-min basis history
    _mids                                   — mid-price deque for vol est.
    _bars, _parkinson_hist                  — regime detector history
    pending                                 — passive order state
    last_features                           — output of the last _features()

If _evaluate_ssf_l5_srm() recreates SsfL5SrmStrategy() on every candle-
close, ALL of the above reset to zero/empty, making the z-score warm-up
period (z_warmup=60 observations by default) unreachable in practice.

This module owns one SsfL5SrmStrategy per symbol for the lifetime of the
stream session. It is instantiated lazily on first use and cleared when
the stream stops.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

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

    def __init__(self, strategy_config: Any) -> None:
        self._lock = threading.Lock()
        self._strategy_config = strategy_config
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
        )

        with self._lock:
            # Double-check: another thread may have inserted one while we
            # were constructing ours.
            if clean not in self._strategies:
                self._strategies[clean] = strategy
                logger.debug(
                    "[SSFLiveRuntime] Created persistent strategy for %s",
                    clean,
                )
            return self._strategies[clean]

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
