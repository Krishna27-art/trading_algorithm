"""
SSF One-Minute Runtime — maintains real 1-minute candle series for:
  1. SSF-L5-SRM volatility/regime detection (60+ 1m candles for EWMA vol & regime filters)
  2. 30-minute return calculations for stock & sector index (for Sector Residual Momentum)

Architecture
------------
  - Uses MultiSymbolCandleAggregator(timeframe_minutes=1, require_vwap_for_callback=False)
  - Seeds historical 1m bars at startup so regime is ready immediately
  - On 1m candle close:
      * Updates SSFReturnTracker with stock and index close
      * Updates SSFLiveRuntime with stock 1m candle
      * Computes causal 30m returns and updates SSFContextStore
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any, Dict, List, Optional, Set

import pandas as pd

from data.candle_aggregator import MultiSymbolCandleAggregator
from data.historical_loader import HistoricalDataLoader
from data.instrument_resolver import instrument_resolver
from data.sector_peer_manager import get_sector_index_symbol
from data.time_utils import now_ist_naive
from monitoring.logger import logger
from streaming.ssf_live_runtime import ssf_live_runtime
from streaming.ssf_market_context import ssf_context_store, ssf_return_tracker


class SSFOneMinuteRuntime:
    """
    Coordinates 1-minute candle processing and seeding for SSF-L5-SRM.
    """

    def __init__(self) -> None:
        self._aggregator: Optional[MultiSymbolCandleAggregator] = None
        self._stock_symbols: Set[str] = set()
        self._index_symbols: Set[str] = set()
        self._symbol_to_index: Dict[str, str] = {}
        self._running = False

    def initialize(
        self,
        symbols: List[str],
        kite_client: Optional[Any] = None,
        seed_history: bool = True,
    ) -> None:
        """
        Initialize the 1-minute runtime for the given stock symbols.
        """
        self._stock_symbols = {s.strip().upper() for s in symbols}
        self._index_symbols = set()
        self._symbol_to_index = {}

        for sym in self._stock_symbols:
            idx = get_sector_index_symbol(sym)
            self._symbol_to_index[sym] = idx
            self._index_symbols.add(idx)

        all_symbols = list(self._stock_symbols | self._index_symbols)

        # Build token_to_symbol map
        token_to_sym: Dict[int, str] = {}
        for sym in all_symbols:
            tok = None
            if kite_client is not None:
                try:
                    tok = instrument_resolver.resolve_token(sym, exchange="NSE", kite_client=kite_client)
                except Exception:
                    tok = None
            if tok is None:
                tok = abs(hash(sym)) % 10000000
            token_to_sym[int(tok)] = sym

        # 1-minute aggregator with require_vwap_for_callback=False (indices don't have VWAP)
        self._aggregator = MultiSymbolCandleAggregator(
            token_to_symbol_map=token_to_sym,
            timeframe_minutes=1,
            on_candle_close=self._on_one_minute_candle_close,
            require_vwap_for_callback=False,
        )

        if seed_history and kite_client is not None:
            self._seed_historical_data(kite_client)

        self._running = True
        logger.info(
            "[SSFOneMinuteRuntime] Initialized for %d stocks and %d sector indices.",
            len(self._stock_symbols),
            len(self._index_symbols),
        )

    def _seed_historical_data(self, kite_client: Any) -> None:
        """
        Fetch real 1-minute candles from Kite for regime and return seeding.
        """
        today = now_ist_naive().date()
        start_date = today - timedelta(days=2)

        for sym in self._stock_symbols:
            try:
                token = instrument_resolver.resolve_token(sym, exchange="NSE", kite_client=kite_client)
                if not token:
                    continue
                df = HistoricalDataLoader.fetch_real_data(
                    kite_client=kite_client,
                    instrument_token=token,
                    start_date=start_date,
                    end_date=today,
                    interval="minute",
                )
                if df is not None and not df.empty:
                    ssf_live_runtime.seed_regime_history(sym, df)
                    ssf_return_tracker.seed(sym, df)
            except Exception as exc:
                logger.debug("[SSFOneMinuteRuntime] Failed to seed 1m history for stock %s: %s", sym, exc)

        for idx in self._index_symbols:
            try:
                token = instrument_resolver.resolve_token(idx, exchange="NSE", kite_client=kite_client)
                if not token:
                    continue
                df = HistoricalDataLoader.fetch_real_data(
                    kite_client=kite_client,
                    instrument_token=token,
                    start_date=start_date,
                    end_date=today,
                    interval="minute",
                )
                if df is not None and not df.empty:
                    ssf_return_tracker.seed(idx, df)
            except Exception as exc:
                logger.debug("[SSFOneMinuteRuntime] Failed to seed 1m history for index %s: %s", idx, exc)

    def on_tick(self, tick: Dict[str, Any]) -> None:
        """Feed tick to the 1-minute aggregator."""
        if not self._running or self._aggregator is None:
            return
        try:
            self._aggregator.process_ticks([tick] if isinstance(tick, dict) else tick)
        except Exception as exc:
            logger.debug("[SSFOneMinuteRuntime] Tick processing error: %s", exc)

    def _on_one_minute_candle_close(self, candle: Dict[str, Any], vwap: Optional[float] = None) -> None:
        """
        Callback fired on every 1-minute candle completion.
        """
        symbol = str(candle.get("symbol", "")).strip().upper()
        if not symbol:
            return

        candle_ts = candle.get("datetime")
        if isinstance(candle_ts, str):
            try:
                candle_ts = datetime.fromisoformat(candle_ts)
            except ValueError:
                candle_ts = now_ist_naive()
        elif not isinstance(candle_ts, datetime):
            candle_ts = now_ist_naive()

        close_px = float(candle.get("close", 0.0))

        # 1. Update return tracker
        ssf_return_tracker.update(symbol, candle_ts, close_px)

        # 2. If it's a stock symbol:
        if symbol in self._stock_symbols:
            ssf_live_runtime.on_one_minute_candle(symbol, candle, vwap)

            # Update sector and stock returns in SSFContextStore
            idx_sym = self._symbol_to_index.get(symbol)
            stock_ret = ssf_return_tracker.get(symbol)
            sector_ret = ssf_return_tracker.get(idx_sym) if idx_sym else None

            ssf_context_store.update_sector_returns(
                symbol=symbol,
                sector_ret_30m=sector_ret,
                stock_ret_30m=stock_ret,
                timestamp=candle_ts,
            )

    def stop(self) -> None:
        """Stop and reset the 1-minute runtime."""
        self._running = False
        self._aggregator = None
        self._stock_symbols.clear()
        self._index_symbols.clear()
        self._symbol_to_index.clear()
        logger.info("[SSFOneMinuteRuntime] Stopped.")


# Global singleton
ssf_one_minute_runtime = SSFOneMinuteRuntime()
