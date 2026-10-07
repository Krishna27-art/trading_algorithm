"""
CRSD Live Runtime — Central live peer context manager for CRSD.

Maintains and updates live CRSDPeerContext instances across symbols,
subscribing to completed 15m candles from peers and the market index (NIFTY).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional

import pandas as pd

from backend.config.settings import settings
from backend.data.sector_peer_manager import SectorPeerManager
from backend.monitoring.logger import logger
from backend.strategy.crsd_strategy import CRSDPeerContext, build_crsd_context


class CRSDLiveRuntime:
    """
    Thread-safe manager for live CRSDPeerContext instances.

    Responsibilities:
    - Maintains one live CRSDPeerContext per active stock/sector.
    - Updates contexts in real time as completed 15m candles arrive for
      peers, the target stock, or the market index (NIFTY).
    - Ensures causal, fail-closed bar tracking so CRSD never operates on
      stale or misaligned peer data.
    """

    def __init__(self, cache_dir: Optional[Path] = None):
        self._lock = threading.Lock()
        self.cache_dir = cache_dir or (settings.base_dir / "backend" / "data" / "cache")
        self._contexts: Dict[str, CRSDPeerContext] = {}

    def get_context(
        self,
        symbol: str,
        kite_client: Optional[Any] = None,
    ) -> Optional[CRSDPeerContext]:
        """Return or lazily construct the CRSDPeerContext for a symbol."""
        clean = symbol.strip().upper()
        with self._lock:
            if clean in self._contexts:
                return self._contexts[clean]

        # Build context using authoritative SectorPeerManager
        ctx = build_crsd_context(
            symbol=clean,
            cache_dir=self.cache_dir,
            kite_client=kite_client,
        )
        if ctx is not None:
            with self._lock:
                # Double-check in case another thread initialized it in parallel
                if clean in self._contexts:
                    return self._contexts[clean]
                self._contexts[clean] = ctx
        return ctx

    def on_candle(self, symbol: str, candle: Dict[str, Any]) -> None:
        """
        Feed a completed 15m candle for `symbol` into all active contexts.
        If `symbol` is NIFTY (or index), updates the '__market__' frame.
        """
        sym = symbol.strip().upper()
        is_market = sym in ("NIFTY", "NIFTY 50", "NIFTY50", "__MARKET__")

        with self._lock:
            contexts = list(self._contexts.values())

        for ctx in contexts:
            try:
                if is_market:
                    ctx.update("__market__", candle)
                elif sym in ctx.frames:
                    ctx.update(sym, candle)
            except Exception as exc:
                logger.warning(f"[CRSDLiveRuntime] Failed to update bar for {sym}: {exc}")

    def update_stock_candle(self, candle_dict: Dict[str, Any]) -> None:
        """Feed a completed 15m stock candle into all active contexts."""
        symbol = str(candle_dict.get("symbol", "")).strip().upper()
        if symbol:
            self.on_candle(symbol, candle_dict)

    def update_market_candle(self, candle_dict: Dict[str, Any]) -> None:
        """Feed a completed 15m NIFTY market candle into all active contexts."""
        self.on_candle("__market__", candle_dict)

    def reset(self) -> None:
        """Clear all in-memory contexts when stream session ends."""
        with self._lock:
            self._contexts.clear()


crsd_live_runtime = CRSDLiveRuntime()
