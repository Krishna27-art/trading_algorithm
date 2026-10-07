"""
SSF Runtime — combined market context, return tracker, live runtime registry,
and 1-minute candle processing for SSF-L5-SRM.
"""

from __future__ import annotations

import logging
import math
import threading
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

import pandas as pd

from backend.config.settings import settings
from backend.config.universe import create_instrument_config_for_equity
from backend.data.candle_aggregator import MultiSymbolCandleAggregator
from backend.data.historical_loader import HistoricalDataLoader
from backend.data.instrument_resolver import instrument_resolver
from backend.data.sector_peer_manager import get_sector_index_symbol
from backend.data.time_utils import IST, now_ist_naive
from backend.monitoring.logger import logger
from backend.strategy.ssf_l5_srm_strategy import SsfL5SrmStrategy


@dataclass
class SSFMarketContext:
    """Per-symbol snapshot of the non-cash data SSF needs."""

    fut_ltp: Optional[float] = None
    fut_oi: Optional[float] = None
    sector_ret_30m: Optional[float] = None
    stock_ret_30m: Optional[float] = None
    circuit_lower: Optional[float] = None
    circuit_upper: Optional[float] = None
    futures_updated_at: Optional[datetime] = None
    sector_return_updated_at: Optional[datetime] = None
    stock_return_updated_at: Optional[datetime] = None
    last_updated: Optional[datetime] = None


class SSFReturnTracker:
    """
    Tracks real 1-minute closes and computes causal 30-minute returns.
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
        try:
            num_close = float(close)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(num_close) or num_close <= 0:
            return None
        with self._lock:
            series = self._series(key)
            series.append((timestamp, num_close))
            target_time = timestamp.timestamp() - 30 * 60
            reference = None
            for ts, price in reversed(series):
                if ts.timestamp() <= target_time:
                    reference = price
                    break
            if reference is None or not math.isfinite(reference) or reference <= 0:
                result = None
            else:
                result = (num_close / reference) - 1.0
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


ssf_return_tracker = SSFReturnTracker()


class SSFContextStore:
    """
    Thread-safe store of SSFMarketContext keyed by symbol.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contexts: Dict[str, SSFMarketContext] = {}

    def _get_or_create(self, symbol: str) -> SSFMarketContext:
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
        clean = str(symbol).strip().upper()
        try:
            num_ltp = float(fut_ltp) if fut_ltp is not None else None
            num_oi = float(fut_oi) if fut_oi is not None else None
            valid_ltp = num_ltp is not None and math.isfinite(num_ltp) and num_ltp > 0
            valid_oi = num_oi is not None and math.isfinite(num_oi) and num_oi >= 0
        except (TypeError, ValueError):
            valid_ltp = False
            valid_oi = False

        if not (valid_ltp and valid_oi):
            logger.warning(
                "[SSFContextStore] Rejecting incomplete futures context for %s: fut_ltp=%r fut_oi=%r",
                clean, fut_ltp, fut_oi,
            )
            return

        now = timestamp or now_ist_naive()

        with self._lock:
            ctx = self._get_or_create(clean)
            ctx.fut_ltp = num_ltp
            ctx.fut_oi = num_oi
            ctx.futures_updated_at = now
            ctx.last_updated = now

    def update_sector_returns(
        self,
        symbol: str,
        sector_ret_30m: Optional[float],
        stock_ret_30m: Optional[float],
        timestamp: Optional[datetime] = None,
    ) -> None:
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
        clean = str(symbol).strip().upper()
        with self._lock:
            ctx = self._get_or_create(clean)
            if circuit_lower is not None:
                ctx.circuit_lower = float(circuit_lower)
            if circuit_upper is not None:
                ctx.circuit_upper = float(circuit_upper)

    def get(self, symbol: str) -> SSFMarketContext:
        clean = str(symbol).strip().upper()
        with self._lock:
            ctx = self._contexts.get(clean)
            if ctx is None:
                return SSFMarketContext()
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
        with self._lock:
            self._contexts.clear()
        ssf_return_tracker.reset()


ssf_context_store = SSFContextStore()


class SSFLiveRuntime:
    """
    Thread-safe registry of persistent SsfL5SrmStrategy instances.
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
        clean = str(symbol).strip().upper()

        with self._lock:
            existing = self._strategies.get(clean)
            if existing is not None:
                return existing

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
                strategy.on_candle(candle, None)
                count += 1
            except (TypeError, ValueError, KeyError):
                continue
        return count

    def on_one_minute_candle(
        self,
        symbol: str,
        token: int,
        candle: dict,
        vwap: Optional[float] = None,
    ) -> None:
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
        with self._lock:
            strategies = list(self._strategies.values())
        for strategy in strategies:
            strategy.reset_session(session_date)

    def reset_symbol(self, symbol: str) -> None:
        clean = str(symbol).strip().upper()
        with self._lock:
            removed = self._strategies.pop(clean, None)
        if removed is not None:
            logger.debug(
                "[SSFLiveRuntime] Cleared persistent strategy for %s",
                clean,
            )

    def reset(self) -> None:
        with self._lock:
            count = len(self._strategies)
            self._strategies.clear()
        logger.info(
            "[SSFLiveRuntime] Cleared %d persistent SSF strategies.",
            count,
        )


ssf_live_runtime = SSFLiveRuntime()


class SSFOneMinuteRuntime:
    """
    Coordinates 1-minute candle processing and seeding for SSF-L5-SRM.
    """

    def __init__(self) -> None:
        self._aggregator: Optional[MultiSymbolCandleAggregator] = None
        self._stock_symbols: Set[str] = set()
        self._index_symbols: Set[str] = set()
        self._symbol_to_index: Dict[str, str] = {}
        self._symbol_to_token: Dict[str, int] = {}
        self._running = False

    def initialize(
        self,
        symbols: List[str],
        kite_client: Optional[Any] = None,
        seed_history: bool = True,
        pre_resolved_token_map: Optional[Dict[int, str]] = None,
    ) -> None:
        self._stock_symbols = {s.strip().upper() for s in symbols}
        self._index_symbols = set()
        self._symbol_to_index = {}
        self._symbol_to_token = {}

        self._index_symbols.add("NIFTY 50")
        self._index_symbols.add("NIFTY")

        for sym in self._stock_symbols:
            idx = get_sector_index_symbol(sym)
            if idx is not None:
                self._symbol_to_index[sym] = idx
                self._index_symbols.add(idx)
            else:
                logger.debug(
                    "[SSFOneMinuteRuntime] No authoritative sector index for %s; sector context unavailable.",
                    sym,
                )

        all_symbols = list(self._stock_symbols | self._index_symbols)
        token_to_sym: Dict[int, str] = {}

        if pre_resolved_token_map:
            for tok, sym in pre_resolved_token_map.items():
                if tok > 0 and sym in all_symbols:
                    token_to_sym[tok] = sym
                    self._symbol_to_token[sym] = tok

        # Resolve any remaining symbols if not in pre_resolved_token_map
        unresolved = [sym for sym in all_symbols if sym not in self._symbol_to_token]
        for sym in unresolved:
            tok = None
            if kite_client is None:
                logger.warning(
                    "[SSFOneMinuteRuntime] No Kite client; cannot resolve token for %s",
                    sym,
                )
                continue

            try:
                tok = instrument_resolver.resolve_token(
                    sym,
                    exchange="NSE",
                    kite_client=kite_client,
                )
            except Exception as exc:
                logger.error(
                    "[SSFOneMinuteRuntime] Token resolution failed for %s: %s: %s",
                    sym, type(exc).__name__, exc,
                )
                continue

            if tok is None:
                logger.error(
                    "[SSFOneMinuteRuntime] No real NSE Kite token resolved for %s; symbol will not be subscribed.",
                    sym,
                )
                continue

            try:
                real_token = int(tok)
            except (TypeError, ValueError):
                logger.error("[SSFOneMinuteRuntime] Invalid Kite token for %s: %r", sym, tok)
                continue

            if real_token <= 0:
                logger.error("[SSFOneMinuteRuntime] Non-positive Kite token for %s: %s", sym, real_token)
                continue

            existing = token_to_sym.get(real_token)
            if existing is not None and existing != sym:
                logger.error(
                    "[SSFOneMinuteRuntime] Kite token collision: token=%s already belongs to %s, rejecting %s.",
                    real_token, existing, sym,
                )
                continue

            token_to_sym[real_token] = sym
            self._symbol_to_token[sym] = real_token

        if not token_to_sym:
            raise RuntimeError(
                "SSFOneMinuteRuntime cannot start: no valid real Kite instrument tokens were resolved."
            )

        resolved_stock_count = sum(
            1 for sym in self._stock_symbols if any(mapped_sym == sym for mapped_sym in token_to_sym.values())
        )
        resolved_index_count = sum(
            1 for sym in self._index_symbols if any(mapped_sym == sym for mapped_sym in token_to_sym.values())
        )

        self._aggregator = MultiSymbolCandleAggregator(
            token_to_symbol_map=token_to_sym,
            timeframe_minutes=1,
            on_candle_close=self._on_one_minute_candle_close,
            require_vwap_for_callback=False,
        )

        self._running = True
        if seed_history and kite_client is not None:
            self._seed_historical_data(kite_client)
        logger.info(
            "[SSFOneMinuteRuntime] Real token resolution: %d/%d stocks, %d/%d sector indices.",
            resolved_stock_count, len(self._stock_symbols),
            resolved_index_count, len(self._index_symbols),
        )

    def _seed_historical_data(self, kite_client: Any) -> None:
        from concurrent.futures import ThreadPoolExecutor

        today = now_ist_naive().date()
        start_date = today - timedelta(days=2)

        stock_symbols = list(self._stock_symbols)
        index_symbols = list(self._index_symbols)

        def _seed_stock(sym: str) -> None:
            if not self._running:
                return
            try:
                token = self._symbol_to_token.get(sym) or instrument_resolver.resolve_token(sym, exchange="NSE", kite_client=kite_client)
                if not token:
                    return
                df = HistoricalDataLoader.fetch_real_data(
                    kite_client=kite_client,
                    instrument_token=token,
                    start_date=start_date,
                    end_date=today,
                    interval="minute",
                )
                if df is None or df.empty:
                    return
                latest_completed_minute = (
                    now_ist_naive().replace(second=0, microsecond=0) - timedelta(minutes=1)
                )
                df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
                df = df.dropna(subset=["datetime"])
                df = df[df["datetime"] <= latest_completed_minute].copy()
                if df.empty:
                    return
                ssf_live_runtime.seed_regime_history(sym, int(token), df)
                ssf_return_tracker.seed(sym, df)
            except Exception as exc:
                logger.debug("[SSFOneMinuteRuntime] Failed to seed 1m history for stock %s: %s", sym, exc)

        def _seed_index(idx: str) -> None:
            if not self._running:
                return
            try:
                token = self._symbol_to_token.get(idx) or instrument_resolver.resolve_token(idx, exchange="NSE", kite_client=kite_client)
                if not token:
                    return
                df = HistoricalDataLoader.fetch_real_data(
                    kite_client=kite_client,
                    instrument_token=token,
                    start_date=start_date,
                    end_date=today,
                    interval="minute",
                )
                if df is None or df.empty:
                    return
                latest_completed_minute = (
                    now_ist_naive().replace(second=0, microsecond=0) - timedelta(minutes=1)
                )
                df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
                df = df.dropna(subset=["datetime"])
                df = df[df["datetime"] <= latest_completed_minute].copy()
                if df.empty:
                    return
                ssf_return_tracker.seed(idx, df)
            except Exception as exc:
                logger.debug("[SSFOneMinuteRuntime] Failed to seed 1m history for index %s: %s", idx, exc)

        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="ssf-seed") as executor:
            list(executor.map(_seed_stock, stock_symbols))
            list(executor.map(_seed_index, index_symbols))

    def seed_historical_data(self, kite_client: Any) -> None:
        self._seed_historical_data(kite_client)

    def on_tick(self, tick: Dict[str, Any]) -> None:
        if not self._running or self._aggregator is None:
            return
        try:
            self._aggregator.process_ticks([tick] if isinstance(tick, dict) else tick)
        except Exception as exc:
            logger.debug("[SSFOneMinuteRuntime] Tick processing error: %s", exc)

    def _on_one_minute_candle_close(self, candle: Dict[str, Any], vwap: Optional[float] = None) -> None:
        symbol = str(candle.get("symbol", "")).strip().upper()
        if not symbol:
            return

        candle_ts = candle.get("datetime")
        if isinstance(candle_ts, str):
            try:
                candle_ts = datetime.fromisoformat(candle_ts)
            except (TypeError, ValueError):
                logger.warning("[SSFOneMinuteRuntime] Rejecting 1m candle with invalid ts for %s: %r", symbol, candle_ts)
                return
        elif not isinstance(candle_ts, datetime):
            logger.warning("[SSFOneMinuteRuntime] Rejecting 1m candle with missing ts for %s: %r", symbol, candle_ts)
            return

        if candle_ts.tzinfo is not None:
            try:
                candle_ts = candle_ts.astimezone(IST).replace(tzinfo=None)
            except Exception:
                logger.warning("[SSFOneMinuteRuntime] Failed to convert ts to IST for %s: %r", symbol, candle_ts)
                return

        close_px = float(candle.get("close", 0.0))
        ssf_return_tracker.update(symbol, candle_ts, close_px)

        if symbol in self._stock_symbols:
            token = self._symbol_to_token.get(symbol)
            if token is None or token <= 0:
                logger.warning("[SSFOneMinuteRuntime] No valid token for stock %s; skipping SSF regime update.", symbol)
                return

            ssf_live_runtime.on_one_minute_candle(symbol, token, candle, vwap)

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
        self._running = False
        self._aggregator = None
        self._stock_symbols.clear()
        self._index_symbols.clear()
        self._symbol_to_index.clear()
        self._symbol_to_token.clear()
        logger.info("[SSFOneMinuteRuntime] Stopped.")


ssf_one_minute_runtime = SSFOneMinuteRuntime()
