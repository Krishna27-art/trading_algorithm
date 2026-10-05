"""
SSF Market Context — per-symbol store of the live data fields that SSF-L5-SRM
requires beyond the standard cash order book.

SSF needs three data sources that do NOT come from the same tick as the cash
stock:

    fut_ltp          — near-month futures last traded price
    fut_oi           — near-month futures open interest
    sector_ret_30m   — sector index 30-minute return
    stock_ret_30m    — stock own 30-minute return (for the sector-residual z)

Without real futures and sector data the SSF basis feature stays at z_basis=0
and the entry condition  z_basis >= basis_z_thr  can never be satisfied.

This module keeps the latest available value of each field so that the
candle-close evaluator and the book-update handler can merge them into a
BookSnapshot before passing it to SsfL5SrmStrategy.on_book_update().

Architecture
------------
  Kite CASH tick        → _on_book_update()        → cash L5 fields
  Kite FUTURES tick     → ssf_context_store         → fut_ltp / fut_oi
  SSFOneMinuteRuntime   → ssf_context_store         → sector_ret_30m / stock_ret_30m
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Deque, Dict, Optional, Tuple

from data.time_utils import now_ist_naive
from monitoring.logger import logger


@dataclass
class SSFMarketContext:
    """Per-symbol snapshot of the non-cash data SSF needs."""

    # Near-month futures
    fut_ltp: Optional[float] = None
    fut_oi: Optional[float] = None

    # 30-minute returns (for sector-residual feature)
    sector_ret_30m: Optional[float] = None
    stock_ret_30m: Optional[float] = None

    # Circuit limits (cash stock)
    circuit_lower: Optional[float] = None
    circuit_upper: Optional[float] = None

    # Per-field timestamps so is_ready() can check freshness independently.
    futures_updated_at: Optional[datetime] = None
    sector_return_updated_at: Optional[datetime] = None
    stock_return_updated_at: Optional[datetime] = None
    last_updated: Optional[datetime] = None


class SSFContextStore:
    """
    Thread-safe store of SSFMarketContext keyed by symbol.

    Each field is updated independently so that cash, futures, and sector
    data sources can write at their own frequencies without contention.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contexts: Dict[str, SSFMarketContext] = {}

    def _get_or_create(self, symbol: str) -> SSFMarketContext:
        """Must be called with self._lock held."""
        clean = str(symbol).strip().upper()
        ctx = self._contexts.get(clean)
        if ctx is None:
            ctx = SSFMarketContext()
            self._contexts[clean] = ctx
        return ctx

    def update_futures(
        self,
        symbol: str,
        fut_ltp: Optional[float],
        fut_oi: Optional[float],
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Record the latest near-month futures LTP and OI for *symbol*."""
        clean = str(symbol).strip().upper()
        try:
            valid_ltp = (
                fut_ltp is not None
                and float(fut_ltp) > 0
            )

            valid_oi = (
                fut_oi is not None
                and float(fut_oi) >= 0
            )
        except (TypeError, ValueError):
            valid_ltp = False
            valid_oi = False

        if not (
            valid_ltp
            and valid_oi
        ):
            logger.warning(
                "[SSFContextStore] Rejecting incomplete "
                "futures context for %s: "
                "fut_ltp=%r fut_oi=%r",
                clean,
                fut_ltp,
                fut_oi,
            )
            return

        now = timestamp or now_ist_naive()

        with self._lock:
            ctx = self._get_or_create(clean)

            ctx.fut_ltp = float(
                fut_ltp
            )

            ctx.fut_oi = float(
                fut_oi
            )

            ctx.futures_updated_at = now
            ctx.last_updated = now

    def update_sector_returns(
        self,
        symbol: str,
        sector_ret_30m: Optional[float],
        stock_ret_30m: Optional[float],
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Record the latest 30-minute sector and stock returns for *symbol*."""
        clean = str(symbol).strip().upper()
        now = timestamp or now_ist_naive()
        with self._lock:
            ctx = self._get_or_create(clean)
            if sector_ret_30m is not None:
                ctx.sector_ret_30m = float(sector_ret_30m)
                ctx.sector_return_updated_at = now
            if stock_ret_30m is not None:
                ctx.stock_ret_30m = float(stock_ret_30m)
                ctx.stock_return_updated_at = now
            ctx.last_updated = now

    def update_circuit_limits(
        self,
        symbol: str,
        circuit_lower: Optional[float],
        circuit_upper: Optional[float],
    ) -> None:
        """Record the circuit breaker limits from a live tick."""
        clean = str(symbol).strip().upper()
        with self._lock:
            ctx = self._get_or_create(clean)
            if circuit_lower is not None:
                ctx.circuit_lower = float(circuit_lower)
            if circuit_upper is not None:
                ctx.circuit_upper = float(circuit_upper)

    def get(self, symbol: str) -> SSFMarketContext:
        """Return the context for *symbol* (or a blank one if not yet seen)."""
        clean = str(symbol).strip().upper()
        with self._lock:
            ctx = self._contexts.get(clean)
            if ctx is None:
                return SSFMarketContext()
            # Return a shallow copy so the caller cannot mutate shared state.
            return SSFMarketContext(
                fut_ltp=ctx.fut_ltp,
                fut_oi=ctx.fut_oi,
                sector_ret_30m=ctx.sector_ret_30m,
                stock_ret_30m=ctx.stock_ret_30m,
                circuit_lower=ctx.circuit_lower,
                circuit_upper=ctx.circuit_upper,
                futures_updated_at=ctx.futures_updated_at,
                sector_return_updated_at=ctx.sector_return_updated_at,
                stock_return_updated_at=ctx.stock_return_updated_at,
                last_updated=ctx.last_updated,
            )

    def is_ready(
        self,
        symbol: str,
        reference_time: datetime,
        max_age_seconds: int = 120,
    ) -> bool:
        """
        Return True when all required SSF external fields are present and fresh.

        required: fut_ltp, fut_oi, sector_ret_30m, stock_ret_30m
        fresh: each field's timestamp is within max_age_seconds of reference_time
        """
        ctx = self.get(symbol)
        required_values = (
            ctx.fut_ltp,
            ctx.fut_oi,
            ctx.sector_ret_30m,
            ctx.stock_ret_30m,
        )
        if any(value is None for value in required_values):
            return False

        timestamps = (
            ctx.futures_updated_at,
            ctx.sector_return_updated_at,
            ctx.stock_return_updated_at,
        )
        if any(ts is None for ts in timestamps):
            return False

        return all(
            0 <= (reference_time - ts).total_seconds() <= max_age_seconds
            for ts in timestamps
        )

    def reset(self) -> None:
        """Clear all stored context (call on stream stop)."""
        with self._lock:
            self._contexts.clear()
        ssf_return_tracker.reset()


class SSFReturnTracker:
    """
    Tracks real 1-minute closes and computes causal 30-minute returns.

    Returns are computed as:
        current_close / close_30_minutes_ago - 1

    where close_30_minutes_ago is the latest close whose timestamp is
    at or before (current_timestamp - 30 minutes).
    """

    def __init__(self, max_points: int = 90) -> None:
        self._lock = threading.Lock()
        self._data: Dict[str, Deque[Tuple[datetime, float]]] = {}
        self._latest_returns: Dict[str, Optional[float]] = {}
        self._max_points = max_points

    def _series(self, key: str) -> Deque[Tuple[datetime, float]]:
        series = self._data.get(key)
        if series is None:
            series = deque(maxlen=self._max_points)
            self._data[key] = series
        return series

    def update(
        self,
        key: str,
        timestamp: datetime,
        close: float,
    ) -> Optional[float]:
        close = float(close)
        if close <= 0:
            return None
        with self._lock:
            series = self._series(key)
            series.append((timestamp, close))
            target_time = timestamp.timestamp() - 30 * 60
            reference = None
            for ts, price in reversed(series):
                if ts.timestamp() <= target_time:
                    reference = price
                    break
            if reference is None or reference <= 0:
                result = None
            else:
                result = (close / reference) - 1.0
            self._latest_returns[key] = result
            return result

    def get(self, key: str) -> Optional[float]:
        with self._lock:
            return self._latest_returns.get(key)

    def seed(
        self,
        key: str,
        candles,
    ) -> None:
        if candles is None or candles.empty:
            return
        data = candles.copy()
        for _, row in data.sort_values("datetime").iterrows():
            try:
                ts = row["datetime"]
                if hasattr(ts, "to_pydatetime"):
                    ts = ts.to_pydatetime()
                self.update(key, ts, float(row["close"]))
            except (TypeError, ValueError):
                continue

    def reset(self) -> None:
        with self._lock:
            self._data.clear()
            self._latest_returns.clear()


# Module-level singletons used by LiveSignalEngine, MarketStreamManager,
# and SSFOneMinuteRuntime.
ssf_return_tracker = SSFReturnTracker()
ssf_context_store = SSFContextStore()
