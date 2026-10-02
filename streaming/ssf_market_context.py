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
  Kite CASH tick  → _on_book_update()  → sets SSFMarketContext.cash fields
  Kite FUTURES tick → [future expansion] → sets SSFMarketContext.fut_ltp / fut_oi
  Sector index calc → [future expansion] → sets SSFMarketContext.sector_ret_30m

Until the futures and sector feeds are wired, fut_ltp and fut_oi remain None
and the basis / OI z-scores stay at 0 — but the strategy instance survives and
keeps accumulating mlofi / microprice history.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional


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
        now = timestamp or datetime.utcnow()
        with self._lock:
            ctx = self._get_or_create(clean)
            if fut_ltp is not None:
                ctx.fut_ltp = float(fut_ltp)
            if fut_oi is not None:
                ctx.fut_oi = float(fut_oi)
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
        now = timestamp or datetime.utcnow()
        with self._lock:
            ctx = self._get_or_create(clean)
            if sector_ret_30m is not None:
                ctx.sector_ret_30m = float(sector_ret_30m)
            if stock_ret_30m is not None:
                ctx.stock_ret_30m = float(stock_ret_30m)
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
                last_updated=ctx.last_updated,
            )

    def reset(self) -> None:
        """Clear all stored context (call on stream stop)."""
        with self._lock:
            self._contexts.clear()


# Module-level singleton used by LiveSignalEngine and MarketStreamManager.
ssf_context_store = SSFContextStore()
