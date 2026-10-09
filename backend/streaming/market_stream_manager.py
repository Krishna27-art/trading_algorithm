from __future__ import annotations

import logging
import math
import os
import queue
import threading
import time as _time
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import pandas as pd

from backend.broker.kite_adapter import get_saved_session, get_active_kite
from backend.config.settings import settings
from backend.config.universe import (
    StockUniverse,
    resolve_universe_tokens,
)
from backend.data.candle_aggregator import MultiSymbolCandleAggregator
from backend.data.historical_loader import HistoricalDataLoader
from backend.data.instrument_resolver import instrument_resolver
from backend.data.sector_peer_manager import get_sector_index_symbol
from backend.data.time_utils import now_ist, now_ist_iso, now_ist_naive, to_ist_aware
from backend.database.db import db_manager
from backend.indicators.volume_profile import VolumeProfileEngine, VolumeProfileFacts
from backend.streaming.crsd_live_runtime import crsd_live_runtime
from backend.streaming.live_market_state import live_market_state
from backend.streaming.live_signal_engine import live_signal_engine
from backend.streaming.ssf_runtime import ssf_context_store
from backend.streaming.ssf_runtime import ssf_one_minute_runtime


logger = logging.getLogger("backend.streaming.market_stream_manager")


NSE_SESSION_OPEN = time(9, 15)
NSE_SESSION_CLOSE = time(15, 30)

DEFAULT_EVALUATION_WORKERS = 8
MAX_EVALUATION_WORKERS = 16

EVALUATION_QUEUE_SIZE = 10000
EVALUATION_MAX_AGE_SECONDS = 300
HISTORY_REFRESH_QUEUE_SIZE = 500
L5_QUEUE_SIZE = 5000

HISTORY_MAX_CONCURRENCY = 4
HISTORY_MIN_REQUEST_INTERVAL_SECONDS = 0.34
HISTORY_RATE_LIMIT_RETRIES = 3
HISTORY_RATE_LIMIT_BACKOFF_SECONDS = 1.0
HISTORY_WARMUP_PASSES = 2
HISTORY_WARMUP_RETRY_DELAY_SECONDS = 5.0


class StreamState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


class HistoryState(str, Enum):
    HISTORY_LOADING = "HISTORY_LOADING"
    HISTORY_READY = "HISTORY_READY"
    HISTORY_STALE = "HISTORY_STALE"
    HISTORY_FAILED = "HISTORY_FAILED"


class _EvaluationTask(NamedTuple):
    generation: int
    symbol: str
    candle_start: datetime
    candle_dict: dict
    vwap: float
    kite_client: Any
    queued_at: datetime


class _RequestRateLimiter:
    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval = float(min_interval_seconds)
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = _time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._min_interval
        wait = slot - now
        if wait > 0:
            _time.sleep(wait)


class _ThrottledKiteClient:
    def __init__(self, client: Any, limiter: _RequestRateLimiter) -> None:
        self._client = client
        self._limiter = limiter

    def __getattr__(self, name: str) -> Any:
        if name in ("_client", "_limiter"):
            raise AttributeError(name)
        attribute = getattr(self._client, name)
        if name != "historical_data" or not callable(attribute):
            return attribute
        limiter = self._limiter

        def throttled(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            while True:
                limiter.acquire()
                try:
                    return attribute(*args, **kwargs)
                except Exception as exc:
                    if (
                        attempt >= HISTORY_RATE_LIMIT_RETRIES
                        or "too many requests" not in str(exc).lower()
                    ):
                        raise
                    attempt += 1
                    _time.sleep(HISTORY_RATE_LIMIT_BACKOFF_SECONDS * attempt)

        return throttled


def _safe_positive_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


class MarketStreamManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()

        self.state: StreamState = StreamState.DISCONNECTED
        self.token_to_symbol: Dict[int, str] = {}
        self.symbol_to_token: Dict[str, int] = {}
        self.subscribed_token_count: int = 0
        self.tick_count: int = 0
        self.candle_count: int = 0
        self.last_tick_time: Optional[datetime] = None
        self.last_equity_tick_time: Optional[datetime] = None
        self.last_connect_time: Optional[datetime] = None
        self.last_disconnect_time: Optional[datetime] = None
        self._awaiting_gap_recovery: bool = False
        self.last_error: Optional[str] = None

        self.kws: Optional[Any] = None
        self.aggregator: Optional[MultiSymbolCandleAggregator] = None
        self.index_aggregator: Optional[MultiSymbolCandleAggregator] = None
        self.volume_profile_engine: VolumeProfileEngine = VolumeProfileEngine()

        self.futures_token_to_symbol: Dict[int, str] = {}
        self.index_token_to_symbol: Dict[int, str] = {}
        self.scanner_snapshot: Optional[Any] = None
        self._session_finalized_date: Optional[str] = None

        self._history_state: Dict[str, HistoryState] = {}
        self._history_epoch: Dict[str, int] = {}
        self._history_lock = threading.Lock()
        self._history_io_semaphore = threading.BoundedSemaphore(
            HISTORY_MAX_CONCURRENCY
        )
        self._history_rate_limiter = _RequestRateLimiter(
            HISTORY_MIN_REQUEST_INTERVAL_SECONDS
        )
        self._history_symbol_locks: Dict[str, threading.Lock] = {}
        self._history_symbol_locks_guard = threading.Lock()

        self._first_observed_candle: Dict[
            str,
            Tuple[datetime, datetime],
        ] = {}

        self._stream_generation: int = 0
        self._last_accepted_tick_timestamp_by_token: Dict[int, datetime] = {}

        self._evaluation_worker_count = self._read_worker_count()
        self._evaluation_queue = queue.Queue(maxsize=EVALUATION_QUEUE_SIZE)
        self._evaluation_queue_lock = threading.Lock()
        self._pending_evaluations: Dict[str, _EvaluationTask] = {}
        self._queued_symbols: set[str] = set()
        self._inflight_symbols: set[str] = set()
        self._last_evaluated_candle_start: Dict[str, datetime] = {}
        self._latest_completed_candle_start: Dict[str, datetime] = {}
        self._deferred_candles: Dict[
            str,
            Tuple[int, dict, float, datetime],
        ] = {}

        self._evaluation_workers_started = False
        self._evaluation_worker_threads: List[threading.Thread] = []

        self._evaluation_enqueued_count = 0
        self._evaluation_coalesced_count = 0
        self._evaluation_dropped_count = 0
        self._evaluation_stale_dropped_count = 0
        self._evaluation_inflight = 0

        self._last_evaluation_time: Optional[datetime] = None
        self._last_evaluation_error: Optional[str] = None

        self._ticks_received_count = 0
        self._ticks_accepted_count = 0
        self._ticks_rejected_count = 0
        self._ticks_invalid_count = 0
        self._ticks_stale_count = 0
        self._ticks_out_of_order_count = 0

        self._l5_received_count = 0
        self._l5_processed_count = 0
        self._l5_replaced_count = 0

        self._history_refresh_queue = queue.Queue(
            maxsize=HISTORY_REFRESH_QUEUE_SIZE
        )
        self._history_refresh_lock = threading.Lock()
        self._history_refresh_requested: Dict[str, int] = {}
        self._history_refresh_worker_started = False
        self._history_refresh_thread: Optional[threading.Thread] = None

        self._book_queue = queue.Queue(maxsize=L5_QUEUE_SIZE)
        self._book_lock = threading.Lock()
        self._pending_book_snapshots: Dict[str, Tuple[int, Any]] = {}
        self._dirty_book_symbols: Set[str] = set()
        self._book_worker_started = False
        self._book_worker_thread: Optional[threading.Thread] = None

        self._ssf_seed_thread: Optional[threading.Thread] = None

        self._flush_worker_started = False
        self._flush_worker_thread: Optional[threading.Thread] = None
        self._process_lock_fd: Optional[int] = None
        self._stop_event = threading.Event()

    def _acquire_process_stream_lock(self) -> None:
        if self._process_lock_fd is not None:
            return
        lock_path = settings.base_dir / "backend" / "data" / ".market_stream.lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            import fcntl
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._process_lock_fd = fd
        except (IOError, OSError) as exc:
            raise RuntimeError(
                "Another process already owns the active KiteTicker market stream. "
                "Multi-process concurrent streaming is prohibited."
            ) from exc

    def _release_process_stream_lock(self) -> None:
        fd = self._process_lock_fd
        if fd is not None:
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            except Exception:
                pass
            self._process_lock_fd = None

    def set_scanner_snapshot(self, snapshot: Optional[Any]) -> None:
        with self._lock:
            self.scanner_snapshot = snapshot
        live_signal_engine.set_scanner_snapshot(snapshot)

    def get_scanner_snapshot(self) -> Optional[Any]:
        with self._lock:
            return self.scanner_snapshot

    @staticmethod
    def _read_worker_count() -> int:
        raw = os.getenv(
            "MARKET_STREAM_EVALUATION_WORKERS",
            str(DEFAULT_EVALUATION_WORKERS),
        )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = DEFAULT_EVALUATION_WORKERS
        return max(1, min(value, MAX_EVALUATION_WORKERS))

    def _ensure_background_workers_started(self) -> None:
        self._stop_event.clear()
        if not self._evaluation_workers_started:
            self._evaluation_workers_started = True
            for index in range(self._evaluation_worker_count):
                thread = threading.Thread(
                    target=self._evaluation_worker_loop,
                    name=f"signal-evaluator-{index + 1}",
                    daemon=True,
                )
                thread.start()
                self._evaluation_worker_threads.append(thread)

        if not self._history_refresh_worker_started:
            self._history_refresh_worker_started = True
            self._history_refresh_thread = threading.Thread(
                target=self._history_refresh_worker_loop,
                name="kite-history-refresh",
                daemon=True,
            )
            self._history_refresh_thread.start()

        if not self._book_worker_started:
            self._book_worker_started = True
            self._book_worker_thread = threading.Thread(
                target=self._book_worker_loop,
                name="ssf-l5-worker",
                daemon=True,
            )
            self._book_worker_thread.start()

        if not self._flush_worker_started:
            self._flush_worker_started = True
            self._flush_worker_thread = threading.Thread(
                target=self._flush_worker_loop,
                name="candle-flush-worker",
                daemon=True,
            )
            self._flush_worker_thread.start()

    def _flush_worker_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=1.0):
                break
            agg = self.aggregator
            if agg is not None:
                try:
                    agg.flush_due_candles()
                except Exception:
                    logger.debug(
                        "[MarketStreamManager] Periodic candle flush error",
                        exc_info=True,
                    )
            idx_agg = self.index_aggregator
            if idx_agg is not None:
                try:
                    idx_agg.flush_due_candles()
                except Exception:
                    logger.debug(
                        "[MarketStreamManager] Periodic index candle flush error",
                        exc_info=True,
                    )

    @staticmethod
    def _normalize_exchange_timestamp(value: Any) -> Optional[datetime]:
        if value is None:
            return None
        try:
            ts = pd.Timestamp(value)
        except (TypeError, ValueError):
            return None
        if pd.isna(ts):
            return None
        try:
            if ts.tzinfo is not None:
                ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
        except (TypeError, ValueError):
            return None
        return ts.to_pydatetime()

    @classmethod
    def _extract_exchange_timestamp(
        cls,
        tick: Dict[str, Any],
    ) -> Optional[datetime]:
        raw_timestamp = tick.get("exchange_timestamp") or tick.get("timestamp")
        return cls._normalize_exchange_timestamp(raw_timestamp)

    @staticmethod
    def _candle_bucket_start(
        timestamp: datetime,
        timeframe_minutes: int,
    ) -> Optional[datetime]:
        if timeframe_minutes <= 0:
            raise ValueError("timeframe_minutes must be > 0")

        session_open = datetime.combine(timestamp.date(), NSE_SESSION_OPEN)
        session_close = datetime.combine(timestamp.date(), NSE_SESSION_CLOSE)

        if timestamp < session_open or timestamp >= session_close:
            return None

        elapsed_minutes = int((timestamp - session_open).total_seconds() // 60)
        bucket_offset = (elapsed_minutes // timeframe_minutes) * timeframe_minutes
        return session_open + timedelta(minutes=bucket_offset)

    def _record_first_observed_candle(
        self,
        symbol: str,
        exchange_timestamp: datetime,
        timeframe_minutes: int,
        generation: int,
    ) -> None:
        bucket_start = self._candle_bucket_start(
            exchange_timestamp,
            timeframe_minutes,
        )
        if bucket_start is None:
            return
        with self._lock:
            if generation != self._stream_generation:
                return
            if symbol not in self._first_observed_candle:
                self._first_observed_candle[symbol] = (
                    bucket_start,
                    exchange_timestamp,
                )

    def _consume_partial_first_candle(
        self,
        symbol: str,
        candle_timestamp: datetime,
        generation: int,
    ) -> bool:
        with self._lock:
            if generation != self._stream_generation:
                return False
            marker = self._first_observed_candle.pop(symbol, None)
        if marker is None:
            return False
        bucket_start, first_tick_timestamp = marker
        return (
            candle_timestamp == bucket_start
            and first_tick_timestamp > bucket_start
        )

    def _is_current_generation(self, generation: int) -> bool:
        return generation == self._stream_generation

    def get_history_state(self, symbol: str) -> HistoryState:
        clean_symbol = str(symbol).strip().upper()
        with self._history_lock:
            return self._history_state.get(
                clean_symbol,
                HistoryState.HISTORY_LOADING,
            )

    def is_history_ready(self, symbol: str) -> bool:
        return self.get_history_state(symbol) == HistoryState.HISTORY_READY

    def _set_history_state(
        self,
        symbol: str,
        state: HistoryState,
        generation: int,
        expected_epoch: Optional[int] = None,
    ) -> Optional[HistoryState]:
        with self._lock:
            if generation != self._stream_generation:
                return None
            with self._history_lock:
                current = self._history_state.get(symbol)
                if current == HistoryState.HISTORY_READY and state in (
                    HistoryState.HISTORY_LOADING,
                    HistoryState.HISTORY_FAILED,
                ):
                    return current
                final_state = state
                if (
                    state == HistoryState.HISTORY_READY
                    and expected_epoch is not None
                    and self._history_epoch.get(symbol, 0) != expected_epoch
                ):
                    final_state = HistoryState.HISTORY_STALE
                self._history_state[symbol] = final_state
                return final_state

    def _mark_history_stale(self, symbol: str, generation: int) -> None:
        with self._lock:
            if generation != self._stream_generation:
                return
            with self._history_lock:
                if self._history_state.get(symbol) == HistoryState.HISTORY_READY:
                    self._history_state[symbol] = HistoryState.HISTORY_STALE

    def _mark_all_history_stale(self, generation: int) -> None:
        with self._lock:
            if generation != self._stream_generation:
                return
            with self._history_lock:
                for symbol, state in list(self._history_state.items()):
                    if state == HistoryState.HISTORY_READY:
                        self._history_state[symbol] = HistoryState.HISTORY_STALE

    def _cache_path_for_symbol(self, symbol: str):
        return (
            settings.base_dir
            / "backend"
            / "data"
            / "cache"
            / f"{symbol}_15m.csv"
        )

    def _symbol_history_lock(self, symbol: str) -> threading.Lock:
        with self._history_symbol_locks_guard:
            lock = self._history_symbol_locks.get(symbol)
            if lock is None:
                lock = threading.Lock()
                self._history_symbol_locks[symbol] = lock
            return lock

    def _flush_deferred_candle(
        self,
        symbol: str,
        kite_client: Any,
        generation: int,
    ) -> bool:
        with self._evaluation_queue_lock:
            deferred = self._deferred_candles.pop(symbol, None)
        if deferred is None or deferred[0] != generation:
            return False
        return self._enqueue_evaluation(
            candle_dict=deferred[1],
            vwap=deferred[2],
            kite_client=kite_client,
            generation=generation,
        )

    def _enqueue_after_history_ready(
        self,
        symbol: str,
        df: pd.DataFrame,
        kite_client: Any,
        generation: int,
    ) -> None:
        if self._flush_deferred_candle(symbol, kite_client, generation):
            return

        latest_row = df.iloc[-1].to_dict()
        latest_timestamp = latest_row.get("datetime")
        normalized = self._normalize_exchange_timestamp(latest_timestamp)
        if normalized is None:
            logger.warning(
                "[%s] Historical warm-up produced no timestamp; live evaluation skipped.",
                symbol,
            )
            return

        today = now_ist_naive().date()
        if normalized.date() != today:
            logger.info(
                "[%s] Historical warm-up is not current-session data (latest=%s today=%s); strategy evaluation skipped.",
                symbol,
                latest_timestamp,
                today,
            )
            return

        latest_candle = {
            "symbol": symbol,
            "datetime": latest_timestamp,
            "open": float(latest_row.get("open", 0.0)),
            "high": float(latest_row.get("high", 0.0)),
            "low": float(latest_row.get("low", 0.0)),
            "close": float(latest_row.get("close", 0.0)),
            "volume": int(latest_row.get("volume", 0)),
        }
        latest_vwap = latest_row.get("vwap")
        if latest_vwap is not None:
            try:
                latest_vwap = float(latest_vwap)
            except (TypeError, ValueError):
                latest_vwap = None
        if latest_vwap is not None and latest_vwap <= 0:
            latest_vwap = None

        self._enqueue_evaluation(
            candle_dict=latest_candle,
            vwap=latest_vwap,
            kite_client=kite_client,
            generation=generation,
        )

    def _warm_one_symbol_historical_state(
        self,
        token: int,
        symbol: str,
        kite_client: Any,
        generation: int,
    ) -> bool:
        clean_symbol = str(symbol).strip().upper()
        cache_path = self._cache_path_for_symbol(clean_symbol)

        with self._history_lock:
            epoch = self._history_epoch.get(clean_symbol, 0)

        if (
            self._set_history_state(
                clean_symbol,
                HistoryState.HISTORY_LOADING,
                generation,
            )
            is None
        ):
            return False

        try:
            throttled_client = _ThrottledKiteClient(
                kite_client,
                self._history_rate_limiter,
            )

            with self._symbol_history_lock(clean_symbol):
                if not self._is_current_generation(generation):
                    return False
                with self._history_io_semaphore:
                    df = HistoricalDataLoader.load_or_refresh_intraday_cache(
                        kite_client=throttled_client,
                        instrument_token=int(token),
                        cache_path=cache_path,
                        now=now_ist_naive(),
                        lookback_days=45,
                        interval="15minute",
                    )

            if not self._is_current_generation(generation):
                return False

            if df is None or df.empty:
                logger.warning(
                    "[MarketStreamManager] No historical 15m data available for %s",
                    clean_symbol,
                )
                self._set_history_state(
                    clean_symbol,
                    HistoryState.HISTORY_FAILED,
                    generation,
                )
                return False

            seeded = live_market_state.seed_historical_candles(
                symbol=clean_symbol,
                candles=df,
            )

            if seeded <= 0:
                logger.warning(
                    "[MarketStreamManager] Historical data for %s contained no valid completed candles.",
                    clean_symbol,
                )
                self._set_history_state(
                    clean_symbol,
                    HistoryState.HISTORY_FAILED,
                    generation,
                )
                return False

            final_state = self._set_history_state(
                clean_symbol,
                HistoryState.HISTORY_READY,
                generation,
                expected_epoch=epoch,
            )

            if final_state is None:
                return False

            logger.info(
                "[MarketStreamManager] Seeded %s historical candles for %s (%s)",
                seeded,
                clean_symbol,
                final_state.value,
            )

            if final_state == HistoryState.HISTORY_READY:
                try:
                    self._enqueue_after_history_ready(
                        clean_symbol,
                        df,
                        kite_client,
                        generation,
                    )
                except Exception as eval_exc:
                    logger.debug(
                        "[MarketStreamManager] Initial history evaluation enqueue skipped for %s: %s",
                        clean_symbol,
                        eval_exc,
                    )

            return True

        except Exception as exc:
            self._set_history_state(
                clean_symbol,
                HistoryState.HISTORY_FAILED,
                generation,
            )
            logger.error(
                "[MarketStreamManager] Historical warm-up failed for %s token=%s: %s",
                clean_symbol,
                token,
                exc,
                exc_info=True,
            )
            return False

    def _warm_historical_state(
        self,
        token_to_symbol: Dict[int, str],
        kite_client: Any,
        generation: int,
    ) -> None:
        from concurrent.futures import ThreadPoolExecutor

        remaining = list(token_to_symbol.items())

        def _worker(tok: int, sym: str) -> None:
            if not self._is_current_generation(generation):
                return
            try:
                self._warm_one_symbol_historical_state(
                    token=int(tok),
                    symbol=sym,
                    kite_client=kite_client,
                    generation=generation,
                )
            except Exception:
                logger.exception(
                    "[MarketStreamManager] Historical warm-up worker failed."
                )

        for pass_index in range(HISTORY_WARMUP_PASSES):
            if not remaining or not self._is_current_generation(generation):
                return

            with ThreadPoolExecutor(
                max_workers=HISTORY_MAX_CONCURRENCY,
                thread_name_prefix="history-warm",
            ) as executor:
                futures = [
                    executor.submit(_worker, token, symbol)
                    for token, symbol in remaining
                ]
                for future in futures:
                    future.result()

            remaining = [
                (token, symbol)
                for token, symbol in remaining
                if self.get_history_state(symbol) == HistoryState.HISTORY_FAILED
            ]

            if remaining and pass_index + 1 < HISTORY_WARMUP_PASSES:
                _time.sleep(HISTORY_WARMUP_RETRY_DELAY_SECONDS)

    def _book_worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                symbol = self._book_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                with self._book_lock:
                    entry = self._pending_book_snapshots.pop(symbol, None)
                    if self._dirty_book_symbols:
                        next_sym = self._dirty_book_symbols.pop()
                        try:
                            self._book_queue.put_nowait(next_sym)
                        except queue.Full:
                            self._dirty_book_symbols.add(next_sym)

                if entry is not None:
                    entry_generation, snapshot = entry
                    if self._is_current_generation(entry_generation):
                        self._l5_processed_count += 1
                        live_signal_engine.on_book_update(
                            symbol=symbol,
                            snapshot=snapshot,
                        )
            except Exception:
                logger.exception("[MarketStreamManager] SSF L5 worker failed.")
            finally:
                self._book_queue.task_done()

    def _drain_book_state(self) -> None:
        with self._book_lock:
            self._pending_book_snapshots.clear()
            self._dirty_book_symbols.clear()

    def _seed_ssf_history_background(
        self,
        kite_client: Any,
        generation: int,
    ) -> None:
        try:
            if not self._is_current_generation(generation):
                return
            ssf_one_minute_runtime.seed_historical_data(kite_client)
        except Exception:
            logger.exception(
                "[MarketStreamManager] Background SSF historical seeding failed."
            )

    def _schedule_symbol_history_refresh(
        self,
        symbol: str,
        token: Optional[int],
        kite_client: Any,
        generation: int,
    ) -> bool:
        clean_symbol = str(symbol).strip().upper()

        if token is None:
            logger.error(
                "[MarketStreamManager] Cannot refresh history for %s: missing real instrument token.",
                clean_symbol,
            )
            return False

        if not self._is_current_generation(generation):
            return False

        with self._history_refresh_lock:
            if self._history_refresh_requested.get(clean_symbol) == generation:
                return True

            self._history_refresh_requested[clean_symbol] = generation

            try:
                self._history_refresh_queue.put_nowait(
                    (generation, clean_symbol, int(token), kite_client)
                )
            except queue.Full:
                self._history_refresh_requested.pop(clean_symbol, None)
                logger.error(
                    "[MarketStreamManager] History refresh queue full; cannot recover history for %s.",
                    clean_symbol,
                )
                return False

            with self._history_lock:
                self._history_epoch[clean_symbol] = (
                    self._history_epoch.get(clean_symbol, 0) + 1
                )

            return True

    def _history_refresh_worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._history_refresh_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            generation = None
            symbol = None

            try:
                generation, symbol, token, kite_client = item

                if self._is_current_generation(generation):
                    self._warm_one_symbol_historical_state(
                        token=token,
                        symbol=symbol,
                        kite_client=kite_client,
                        generation=generation,
                    )
            except Exception:
                logger.exception(
                    "[MarketStreamManager] Unhandled history-refresh worker error"
                )
            finally:
                if symbol is not None:
                    with self._history_refresh_lock:
                        if self._history_refresh_requested.get(symbol) == generation:
                            self._history_refresh_requested.pop(symbol, None)
                self._history_refresh_queue.task_done()

    def _enqueue_evaluation(
        self,
        candle_dict: dict,
        vwap: Optional[float],
        kite_client: Any,
        generation: int,
    ) -> bool:
        symbol = str(candle_dict.get("symbol", "")).strip().upper()

        if not symbol:
            logger.error(
                "[MarketStreamManager] Cannot queue candle without a symbol."
            )
            return False

        candle_start = self._normalize_exchange_timestamp(
            candle_dict.get("datetime")
        )
        if candle_start is None:
            logger.error(
                "[MarketStreamManager] Cannot queue candle for %s without a valid datetime.",
                symbol,
            )
            return False

        try:
            vwap_value = float(vwap)
        except (TypeError, ValueError):
            logger.debug(
                "[MarketStreamManager] Skipping evaluation enqueue for %s: invalid vwap.",
                symbol,
            )
            return False

        if not self._is_current_generation(generation):
            return False

        task = _EvaluationTask(
            generation=generation,
            symbol=symbol,
            candle_start=candle_start,
            candle_dict=dict(candle_dict),
            vwap=vwap_value,
            kite_client=kite_client,
            queued_at=now_ist_naive(),
        )

        with self._evaluation_queue_lock:
            last_evaluated = self._last_evaluated_candle_start.get(symbol)
            latest_completed = self._latest_completed_candle_start.get(symbol)

            if (
                last_evaluated is not None and candle_start <= last_evaluated
            ) or (
                latest_completed is not None and candle_start < latest_completed
            ):
                self._evaluation_stale_dropped_count += 1
                return False

            existing = self._pending_evaluations.get(symbol)

            if existing is not None:
                if existing.candle_start > candle_start:
                    self._evaluation_stale_dropped_count += 1
                    return False
                self._pending_evaluations[symbol] = task
                self._evaluation_coalesced_count += 1
                return True

            self._pending_evaluations[symbol] = task

            if symbol in self._inflight_symbols or symbol in self._queued_symbols:
                self._evaluation_enqueued_count += 1
                return True

            try:
                self._evaluation_queue.put_nowait(symbol)
            except queue.Full:
                self._pending_evaluations.pop(symbol, None)
                self._evaluation_dropped_count += 1
                logger.error(
                    "[MarketStreamManager] Evaluation queue full (%d); dropping evaluation task for %s to fail closed.",
                    EVALUATION_QUEUE_SIZE,
                    symbol,
                )
                return False

            self._queued_symbols.add(symbol)
            self._evaluation_enqueued_count += 1
            return True

    def _run_evaluation(self, task: _EvaluationTask) -> None:
        symbol = task.symbol

        with self._lock:
            if task.generation != self._stream_generation:
                return
            self._evaluation_inflight += 1

        try:
            wait_seconds = (now_ist_naive() - task.queued_at).total_seconds()
            if wait_seconds > EVALUATION_MAX_AGE_SECONDS:
                logger.warning(
                    "[MarketStreamManager] Candle evaluation for %s delayed by %.1fs > %ds; skipped.",
                    symbol,
                    wait_seconds,
                    EVALUATION_MAX_AGE_SECONDS,
                )
                return

            if not self.is_history_ready(symbol):
                return

            with self._evaluation_queue_lock:
                last_evaluated = self._last_evaluated_candle_start.get(symbol)
                latest_completed = self._latest_completed_candle_start.get(symbol)

                if (
                    last_evaluated is not None
                    and task.candle_start <= last_evaluated
                ) or (
                    latest_completed is not None
                    and task.candle_start < latest_completed
                ):
                    self._evaluation_stale_dropped_count += 1
                    return

                self._last_evaluated_candle_start[symbol] = task.candle_start

            if task.generation != self._stream_generation:
                return

            try:
                live_signal_engine.on_candle_close(
                    task.candle_dict,
                    task.vwap,
                    kite_client=task.kite_client,
                )

                with self._lock:
                    self._last_evaluation_time = now_ist()
                    self._last_evaluation_error = None

            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                with self._lock:
                    self._last_evaluation_error = f"{symbol}: {error}"
                logger.exception(
                    "[MarketStreamManager] Strategy evaluation failed for %s",
                    symbol,
                )
        finally:
            with self._lock:
                self._evaluation_inflight = max(0, self._evaluation_inflight - 1)

    def _evaluation_worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                symbol = self._evaluation_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                with self._evaluation_queue_lock:
                    self._queued_symbols.discard(symbol)
                    task = self._pending_evaluations.pop(symbol, None)
                    if task is not None:
                        self._inflight_symbols.add(symbol)

                if task is not None:
                    try:
                        self._run_evaluation(task)
                    finally:
                        with self._evaluation_queue_lock:
                            self._inflight_symbols.discard(symbol)
                            if (
                                symbol in self._pending_evaluations
                                and symbol not in self._queued_symbols
                            ):
                                try:
                                    self._evaluation_queue.put_nowait(symbol)
                                    self._queued_symbols.add(symbol)
                                except queue.Full:
                                    self._pending_evaluations.pop(symbol, None)
                                    self._evaluation_dropped_count += 1
            except Exception:
                logger.exception(
                    "[MarketStreamManager] Unhandled evaluation-worker error"
                )
            finally:
                self._evaluation_queue.task_done()

    def _drain_evaluation_queue(self) -> None:
        with self._evaluation_queue_lock:
            self._pending_evaluations.clear()
            self._queued_symbols.clear()
            self._last_evaluated_candle_start.clear()
            self._latest_completed_candle_start.clear()
            self._deferred_candles.clear()

            while True:
                try:
                    self._evaluation_queue.get_nowait()
                    self._evaluation_queue.task_done()
                except queue.Empty:
                    break

    def _drain_history_refresh_queue(self) -> None:
        with self._history_refresh_lock:
            self._history_refresh_requested.clear()

            while True:
                try:
                    self._history_refresh_queue.get_nowait()
                    self._history_refresh_queue.task_done()
                except queue.Empty:
                    break

    def start_stream(
        self,
        token_to_symbol: Optional[Dict[int, str]] = None,
        kite_client: Optional[Any] = None,
        timeframe_minutes: int = 15,
    ) -> Dict[str, Any]:
        if timeframe_minutes <= 0:
            raise ValueError("timeframe_minutes must be > 0")

        if kite_client is None:
            kite_client = get_active_kite()

        if kite_client is None:
            with self._lock:
                self.state = StreamState.ERROR
                self.last_error = (
                    "No active Zerodha Kite client available. "
                    "Please authenticate via Kite Connect."
                )
            raise RuntimeError(self.last_error)

        session = get_saved_session()

        if not session:
            with self._lock:
                self.state = StreamState.ERROR
                self.last_error = (
                    "No active Zerodha Kite session found. "
                    "Please authenticate via Kite login."
                )
            raise RuntimeError(self.last_error)

        api_key = session.get("api_key")
        access_token = session.get("access_token")

        if not api_key or not access_token:
            with self._lock:
                self.state = StreamState.ERROR
                self.last_error = "Invalid session credentials."
            raise RuntimeError(self.last_error)

        if not token_to_symbol:
            token_map = resolve_universe_tokens(kite_client=kite_client)

            token_to_symbol = {
                int(token): str(symbol).strip().upper()
                for symbol, token in token_map.items()
                if token
            }

            expected_symbols = {
                str(record.symbol).strip().upper()
                for record in StockUniverse().all_stocks
            }

            resolved_symbols = {
                str(symbol).strip().upper()
                for symbol in token_to_symbol.values()
            }

            missing_symbols = expected_symbols - resolved_symbols
            unexpected_symbols = resolved_symbols - expected_symbols

            if (
                missing_symbols
                or unexpected_symbols
                or len(resolved_symbols) != len(expected_symbols)
            ):
                with self._lock:
                    self.state = StreamState.ERROR
                    self.last_error = (
                        "Refusing to start market stream because "
                        "the resolved universe is not exactly the required "
                        f"{len(expected_symbols)}-stock universe. "
                        f"resolved={len(resolved_symbols)}, "
                        f"expected={len(expected_symbols)}, "
                        f"missing={len(missing_symbols)}, "
                        f"unexpected={len(unexpected_symbols)}"
                    )
                logger.error("[MarketStreamManager] %s", self.last_error)
                raise RuntimeError(self.last_error)
        else:
            expected_universe = {
                str(record.symbol).strip().upper()
                for record in StockUniverse().all_stocks
            }
            validated_map = {}
            for token, symbol in token_to_symbol.items():
                try:
                    tok_int = int(token)
                    sym_clean = str(symbol).strip().upper()
                except (TypeError, ValueError):
                    continue
                if tok_int <= 0 or not sym_clean:
                    continue
                if sym_clean not in expected_universe:
                    logger.warning(
                        "[MarketStreamManager] Rejecting non-universe symbol in caller-supplied mapping: %s",
                        sym_clean,
                    )
                    continue
                validated_map[tok_int] = sym_clean
            token_to_symbol = validated_map

        if not token_to_symbol:
            with self._lock:
                self.state = StreamState.ERROR
                self.last_error = (
                    "No valid instrument tokens available to start market stream."
                )
            raise ValueError(self.last_error)

        with self._lock:
            if self.kws is not None:
                self._stop_internal()

            self._acquire_process_stream_lock()
            self._stream_generation += 1
            generation = self._stream_generation

            self._first_observed_candle.clear()
            self._drain_evaluation_queue()
            self._drain_history_refresh_queue()
            self._drain_book_state()

        try:
            return self._start_stream_impl(
                token_to_symbol=token_to_symbol,
                kite_client=kite_client,
                timeframe_minutes=timeframe_minutes,
                generation=generation,
                api_key=api_key,
                access_token=access_token,
            )
        except Exception:
            with self._lock:
                self._stop_internal()
            raise

    def _start_stream_impl(
        self,
        token_to_symbol: Dict[int, str],
        kite_client: Any,
        timeframe_minutes: int,
        generation: int,
        api_key: str,
        access_token: str,
    ) -> Dict[str, Any]:
        resolved_token_to_symbol = dict(token_to_symbol)
        resolved_symbol_to_token = {
            sym: tok for tok, sym in resolved_token_to_symbol.items()
        }

        resolved_futures_token_to_symbol: Dict[int, str] = {}

        for sym in resolved_token_to_symbol.values():
            try:
                fut_info = instrument_resolver.find_nearest_single_stock_future(
                    sym,
                    kite_client=kite_client,
                )

                if fut_info is None:
                    continue

                raw_fut_token = fut_info.get("instrument_token")

                try:
                    fut_token = int(raw_fut_token)
                except (TypeError, ValueError):
                    logger.warning(
                        "[MarketStreamManager] Invalid futures instrument token for %s: %r",
                        sym,
                        raw_fut_token,
                    )
                    continue

                if fut_token <= 0:
                    logger.warning(
                        "[MarketStreamManager] Non-positive futures instrument token for %s: %s",
                        sym,
                        fut_token,
                    )
                    continue

                resolved_futures_token_to_symbol[fut_token] = sym

            except Exception as exc:
                logger.debug(
                    "[MarketStreamManager] Failed to resolve futures token for %s: %s",
                    sym,
                    exc,
                )

        resolved_index_token_to_symbol: Dict[int, str] = {}
        _resolved_index_tokens: Dict[str, Optional[int]] = {}

        for sym in resolved_token_to_symbol.values():
            try:
                idx_sym = get_sector_index_symbol(sym)

                if not idx_sym:
                    continue

                if idx_sym in _resolved_index_tokens:
                    idx_tok = _resolved_index_tokens[idx_sym]
                else:
                    idx_tok = instrument_resolver.resolve_token(
                        idx_sym,
                        exchange="NSE",
                        kite_client=kite_client,
                    )
                    _resolved_index_tokens[idx_sym] = idx_tok

                if idx_tok:
                    resolved_index_token_to_symbol[int(idx_tok)] = idx_sym

            except Exception as exc:
                logger.debug(
                    "[MarketStreamManager] Failed to resolve index token for %s: %s",
                    sym,
                    exc,
                )

        try:
            nifty_tok = (
                instrument_resolver.resolve_token(
                    "NIFTY",
                    exchange="NSE",
                    kite_client=kite_client,
                )
                or instrument_resolver.resolve_token(
                    "NIFTY 50",
                    exchange="NSE",
                    kite_client=kite_client,
                )
            )
            if nifty_tok:
                resolved_index_token_to_symbol[int(nifty_tok)] = "NIFTY"
        except Exception as exc:
            logger.debug(
                "[MarketStreamManager] Failed to resolve NIFTY token: %s",
                exc,
            )

        all_tokens = list(
            dict.fromkeys(
                list(resolved_token_to_symbol.keys())
                + list(resolved_futures_token_to_symbol.keys())
                + list(resolved_index_token_to_symbol.keys())
            )
        )

        tokens_to_subscribe = list(all_tokens)

        try:
            self.volume_profile_engine.initialize_universe(resolved_token_to_symbol)
        except Exception as vp_init_exc:
            logger.debug(
                "[MarketStreamManager] VolumeProfile universe init error: %s",
                vp_init_exc,
            )

        callback_generation = generation
        index_symbol_set = set(resolved_index_token_to_symbol.values())

        def _token_for(symbol: str) -> Optional[int]:
            state = live_market_state.get_symbol_state(symbol)
            return (
                (state.token if state is not None else None)
                or resolved_symbol_to_token.get(symbol)
            )

        def _on_candle_close(candle_dict: dict, vwap: float) -> None:
            symbol = str(candle_dict.get("symbol", "")).strip().upper()

            candle_timestamp = self._normalize_exchange_timestamp(
                candle_dict.get("datetime")
            )

            if not symbol or candle_timestamp is None:
                logger.error(
                    "[MarketStreamManager] Rejected malformed candle-close callback: %r",
                    candle_dict,
                )
                return

            with self._lock:
                if callback_generation != self._stream_generation:
                    logger.debug(
                        "[MarketStreamManager] Ignoring stale candle callback from generation %s; current=%s",
                        callback_generation,
                        self._stream_generation,
                    )
                    return
                self.candle_count += 1

            if self._consume_partial_first_candle(
                symbol=symbol,
                candle_timestamp=candle_timestamp,
                generation=callback_generation,
            ):
                logger.warning(
                    "[MarketStreamManager] Ignoring partial first candle for %s at %s. Scheduling real Kite historical backfill.",
                    symbol,
                    candle_timestamp,
                )

                self._mark_history_stale(symbol, callback_generation)

                self._schedule_symbol_history_refresh(
                    symbol=symbol,
                    token=_token_for(symbol),
                    kite_client=kite_client,
                    generation=callback_generation,
                )
                return

            if callback_generation != self._stream_generation:
                return

            live_market_state.update_candle_close(candle_dict, vwap)

            if (
                symbol in ("NIFTY", "NIFTY 50", "NIFTY50", "__MARKET__")
                or symbol in index_symbol_set
            ):
                crsd_live_runtime.update_market_candle(candle_dict)
                return

            crsd_live_runtime.update_stock_candle(candle_dict)

            with self._evaluation_queue_lock:
                latest = self._latest_completed_candle_start.get(symbol)
                if latest is None or candle_timestamp > latest:
                    self._latest_completed_candle_start[symbol] = candle_timestamp

            history_state = self.get_history_state(symbol)

            if history_state != HistoryState.HISTORY_READY:
                with self._evaluation_queue_lock:
                    existing = self._deferred_candles.get(symbol)
                    if existing is None or existing[3] < candle_timestamp:
                        self._deferred_candles[symbol] = (
                            callback_generation,
                            dict(candle_dict),
                            vwap,
                            candle_timestamp,
                        )

                if self.get_history_state(symbol) == HistoryState.HISTORY_READY:
                    self._flush_deferred_candle(
                        symbol,
                        kite_client,
                        callback_generation,
                    )
                    return

                if history_state in (
                    HistoryState.HISTORY_STALE,
                    HistoryState.HISTORY_FAILED,
                ):
                    self._schedule_symbol_history_refresh(
                        symbol=symbol,
                        token=_token_for(symbol),
                        kite_client=kite_client,
                        generation=callback_generation,
                    )

                logger.info(
                    "[MarketStreamManager] Deferring strategy evaluation for %s: history state is %s.",
                    symbol,
                    history_state.value,
                )
                return

            queued = self._enqueue_evaluation(
                candle_dict=candle_dict,
                vwap=vwap,
                kite_client=kite_client,
                generation=callback_generation,
            )

            if not queued:
                logger.debug(
                    "[MarketStreamManager] Completed candle evaluation for %s was not queued.",
                    symbol,
                )

        def _on_book_update(symbol: str, snapshot: Any) -> None:
            if callback_generation != self._stream_generation:
                return

            self._l5_received_count += 1
            live_market_state.update_book_snapshot(symbol, snapshot)

            with self._book_lock:
                if symbol in self._pending_book_snapshots:
                    self._l5_replaced_count += 1
                self._pending_book_snapshots[symbol] = (
                    callback_generation,
                    snapshot,
                )

            try:
                self._book_queue.put_nowait(symbol)
            except queue.Full:
                pass

        aggregator = MultiSymbolCandleAggregator(
            token_to_symbol_map=resolved_token_to_symbol,
            timeframe_minutes=timeframe_minutes,
            on_candle_close=_on_candle_close,
            on_book_update=_on_book_update,
        )

        index_aggregator = None
        if resolved_index_token_to_symbol:
            index_aggregator = MultiSymbolCandleAggregator(
                token_to_symbol_map=resolved_index_token_to_symbol,
                timeframe_minutes=timeframe_minutes,
                on_candle_close=_on_candle_close,
                require_vwap_for_callback=False,
            )

        try:
            from kiteconnect import KiteTicker
        except ImportError:
            with self._lock:
                self.state = StreamState.ERROR
                self.last_error = "kiteconnect package is not installed."
            raise RuntimeError(self.last_error)

        kws = KiteTicker(api_key, access_token)
        try:
            kws.enable_reconnect(reconnect_interval=3, reconnect_tries=50)
        except Exception:
            pass

        def on_ticks(ws, ticks):
            tick_batch = ticks if isinstance(ticks, list) else [ticks]

            with self._lock:
                if callback_generation != self._stream_generation:
                    logger.debug(
                        "[MarketStreamManager] Ignoring stale callback from generation %s; current=%s",
                        callback_generation,
                        self._stream_generation,
                    )
                    return
                self.tick_count += len(tick_batch)
                self._ticks_received_count += len(tick_batch)

            cash_ticks: List[dict] = []
            index_ticks: List[dict] = []
            accepted_tick_count = 0
            latest_exchange_timestamp = None
            latest_equity_timestamp = None

            for tick in tick_batch:
                if not isinstance(tick, dict):
                    continue

                try:
                    tok = int(tick.get("instrument_token"))
                except (TypeError, ValueError):
                    continue

                exchange_timestamp = self._extract_exchange_timestamp(tick)

                if exchange_timestamp is None:
                    continue

                with self._lock:
                    if callback_generation != self._stream_generation:
                        return
                    previous_tick_ts = (
                        self._last_accepted_tick_timestamp_by_token.get(tok)
                    )
                    if (
                        previous_tick_ts is not None
                        and exchange_timestamp < previous_tick_ts
                    ):
                        self._ticks_out_of_order_count += 1
                        logger.warning(
                            "[MarketStreamManager] Dropping out-of-order tick token=%s ts=%s previous=%s",
                            tok,
                            exchange_timestamp,
                            previous_tick_ts,
                        )
                        continue
                    self._last_accepted_tick_timestamp_by_token[tok] = (
                        exchange_timestamp
                    )

                if tok in resolved_futures_token_to_symbol:
                    ssf_context_store.update_futures(
                        symbol=resolved_futures_token_to_symbol[tok],
                        fut_ltp=tick.get("last_price"),
                        fut_oi=tick.get("oi"),
                        timestamp=exchange_timestamp,
                    )
                    accepted_tick_count += 1
                    if (
                        latest_exchange_timestamp is None
                        or exchange_timestamp > latest_exchange_timestamp
                    ):
                        latest_exchange_timestamp = exchange_timestamp
                    continue

                if tok in resolved_index_token_to_symbol:
                    ssf_one_minute_runtime.on_tick(tick)
                    accepted_tick_count += 1
                    if (
                        latest_exchange_timestamp is None
                        or exchange_timestamp > latest_exchange_timestamp
                    ):
                        latest_exchange_timestamp = exchange_timestamp
                    index_ticks.append(tick)
                    continue

                sym = resolved_token_to_symbol.get(tok)
                last_price = tick.get("last_price")

                if sym is None or last_price is None:
                    self._ticks_rejected_count += 1
                    continue

                try:
                    price = float(last_price)
                except (TypeError, ValueError):
                    self._ticks_invalid_count += 1
                    continue

                if not math.isfinite(price) or price <= 0:
                    self._ticks_invalid_count += 1
                    continue

                if callback_generation != self._stream_generation:
                    return

                accepted_tick_count += 1
                if (
                    latest_exchange_timestamp is None
                    or exchange_timestamp > latest_exchange_timestamp
                ):
                    latest_exchange_timestamp = exchange_timestamp

                if (
                    latest_equity_timestamp is None
                    or exchange_timestamp > latest_equity_timestamp
                ):
                    latest_equity_timestamp = exchange_timestamp

                self._record_first_observed_candle(
                    symbol=sym,
                    exchange_timestamp=exchange_timestamp,
                    timeframe_minutes=timeframe_minutes,
                    generation=callback_generation,
                )

                live_signal_engine.record_tick_price(
                    symbol=sym,
                    price=price,
                    timestamp=exchange_timestamp,
                )

                ohlc = tick.get("ohlc") or {}
                day_open = _safe_positive_float(ohlc.get("open"))
                day_high = _safe_positive_float(ohlc.get("high"))
                day_low = _safe_positive_float(ohlc.get("low"))
                raw_session_volume = tick.get("volume_traded")
                try:
                    session_volume = (
                        int(raw_session_volume)
                        if raw_session_volume is not None
                        else None
                    )
                except (TypeError, ValueError):
                    session_volume = None

                live_market_state.update_tick(
                    symbol=sym,
                    price=price,
                    volume=0,
                    timestamp=exchange_timestamp,
                    token=tok,
                    day_open=day_open,
                    day_high=day_high,
                    day_low=day_low,
                    session_volume=session_volume,
                )

                ssf_one_minute_runtime.on_tick(tick)

                cash_ticks.append(tick)

            if callback_generation != self._stream_generation:
                return

            if (
                latest_equity_timestamp is not None
                and latest_equity_timestamp.time() >= time(15, 30)
                and not self._awaiting_gap_recovery
            ):
                today_str = latest_equity_timestamp.strftime("%Y-%m-%d")
                should_finalize = False
                with self._lock:
                    if (
                        callback_generation == self._stream_generation
                        and self._session_finalized_date != today_str
                    ):
                        self._session_finalized_date = today_str
                        should_finalize = True
                if should_finalize:
                    try:
                        latest_prices = {
                            sym: float(st.ltp)
                            for sym, st in live_market_state.get_all_symbols_state().items()
                            if st and st.ltp and math.isfinite(st.ltp) and st.ltp > 0
                        }
                        if latest_prices:
                            db_manager.finalize_session_signals(
                                trading_date=today_str,
                                session_close_time=latest_equity_timestamp,
                                current_prices=latest_prices,
                            )
                        try:
                            self.volume_profile_engine.save_session_profiles()
                        except Exception as vp_fin_exc:
                            logger.debug(
                                "[MarketStreamManager] VolumeProfile session save error: %s",
                                vp_fin_exc,
                            )
                    except Exception as fin_exc:
                        logger.debug(
                            "[MarketStreamManager] 15:30 session finalization error: %s",
                            fin_exc,
                        )

            if cash_ticks:
                try:
                    self.volume_profile_engine.process_ticks(
                        cash_ticks,
                        resolved_token_to_symbol,
                    )
                except Exception:
                    logger.exception(
                        "[MarketStreamManager] Error processing cash ticks in VolumeProfileEngine"
                    )

                if callback_generation != self._stream_generation:
                    return

                try:
                    aggregator.process_ticks(cash_ticks)
                except Exception:
                    logger.exception(
                        "[MarketStreamManager] Error processing cash ticks in CandleAggregator"
                    )

            if index_ticks and index_aggregator is not None:
                if callback_generation == self._stream_generation:
                    try:
                        index_aggregator.process_ticks(index_ticks)
                    except Exception:
                        logger.exception(
                            "[MarketStreamManager] Error processing index ticks in IndexCandleAggregator"
                        )

            with self._lock:
                if callback_generation != self._stream_generation:
                    return
                self._ticks_accepted_count += accepted_tick_count
                if latest_equity_timestamp is not None:
                    self.last_equity_tick_time = latest_equity_timestamp
                if (
                    accepted_tick_count > 0
                    and latest_exchange_timestamp is not None
                ):
                    self.last_tick_time = latest_exchange_timestamp

        def on_connect(ws, response):
            with self._lock:
                if callback_generation != self._stream_generation:
                    logger.debug(
                        "[MarketStreamManager] Ignoring stale callback from generation %s; current=%s",
                        callback_generation,
                        self._stream_generation,
                    )
                    return

                self.state = StreamState.CONNECTED
                self.last_connect_time = now_ist()
                self.last_error = None
                gap_detected = self._awaiting_gap_recovery
                self._awaiting_gap_recovery = False

            ws.subscribe(tokens_to_subscribe)
            ws.set_mode(ws.MODE_FULL, tokens_to_subscribe)

            logger.info(
                "[MarketStreamManager] KiteTicker connected in MODE_FULL — subscribed %d instruments (%d cash, %d futures, %d indices).",
                len(tokens_to_subscribe),
                len(resolved_token_to_symbol),
                len(resolved_futures_token_to_symbol),
                len(resolved_index_token_to_symbol),
            )

            if gap_detected:
                logger.warning(
                    "[MarketStreamManager] Reconnected after a stream gap. Discarding in-progress candles and volume baselines to avoid misattributing missed-tick volume or data."
                )

                self._mark_all_history_stale(callback_generation)

                try:
                    aggregator.handle_connection_gap()
                except Exception:
                    logger.exception(
                        "[MarketStreamManager] Error recovering aggregator state after connection gap."
                    )

                if index_aggregator is not None:
                    try:
                        index_aggregator.handle_connection_gap()
                    except Exception:
                        logger.exception(
                            "[MarketStreamManager] Error recovering index aggregator state after connection gap."
                        )

                try:
                    self.volume_profile_engine.handle_connection_gap()
                except Exception:
                    logger.exception(
                        "[MarketStreamManager] Error recovering volume profile state after connection gap."
                    )

                with self._lock:
                    if callback_generation == self._stream_generation:
                        self._first_observed_candle.clear()

        def on_close(ws, code, reason):
            with self._lock:
                if callback_generation != self._stream_generation:
                    logger.debug(
                        "[MarketStreamManager] Ignoring stale callback from generation %s; current=%s",
                        callback_generation,
                        self._stream_generation,
                    )
                    return

                self.state = StreamState.DISCONNECTED
                self.last_disconnect_time = now_ist()
                self._awaiting_gap_recovery = True

            logger.warning(
                "[MarketStreamManager] KiteTicker closed: code=%s reason=%s",
                code,
                reason,
            )

        def on_error(ws, code, reason):
            error_text = f"code={code} reason={reason}"
            with self._lock:
                if callback_generation != self._stream_generation:
                    logger.debug(
                        "[MarketStreamManager] Ignoring stale callback from generation %s; current=%s",
                        callback_generation,
                        self._stream_generation,
                    )
                    return

                self.state = StreamState.ERROR
                self.last_error = error_text

            logger.error(
                "[MarketStreamManager] KiteTicker error: %s",
                error_text,
            )

            try:
                from backend.broker.kite_adapter import get_active_kite_with_diagnostics

                client, validation_error = get_active_kite_with_diagnostics(
                    force_validate=True
                )

                if client is None and validation_error:
                    logger.error(
                        "[MarketStreamManager] Kite authentication became invalid: %s",
                        validation_error,
                    )
            except Exception:
                logger.exception(
                    "[MarketStreamManager] Failed to revalidate Kite session after stream error."
                )

        def on_reconnect(ws, attempts_count):
            with self._lock:
                if callback_generation != self._stream_generation:
                    logger.debug(
                        "[MarketStreamManager] Ignoring stale callback from generation %s; current=%s",
                        callback_generation,
                        self._stream_generation,
                    )
                    return

                self.state = StreamState.RECONNECTING

            logger.info(
                "[MarketStreamManager] KiteTicker reconnecting (attempt %s)...",
                attempts_count,
            )

        def on_noreconnect(ws):
            with self._lock:
                if callback_generation != self._stream_generation:
                    return
                self.state = StreamState.ERROR
                self.last_error = (
                    "KiteTicker reconnection attempts exhausted. Stream disconnected."
                )

            logger.error(
                "[MarketStreamManager] KiteTicker on_noreconnect: reconnection exhausted."
            )

        kws.on_ticks = on_ticks
        kws.on_connect = on_connect
        kws.on_close = on_close
        kws.on_error = on_error
        kws.on_reconnect = on_reconnect
        kws.on_noreconnect = on_noreconnect

        with self._lock:
            if generation != self._stream_generation:
                try:
                    kws.close()
                except Exception:
                    pass
                return {"status": "STALE_START"}

            self.token_to_symbol = dict(resolved_token_to_symbol)
            self.symbol_to_token = dict(resolved_symbol_to_token)
            self.futures_token_to_symbol = dict(resolved_futures_token_to_symbol)
            self.index_token_to_symbol = dict(resolved_index_token_to_symbol)
            self.subscribed_token_count = len(tokens_to_subscribe)
            self.state = StreamState.CONNECTING
            self.last_error = None
            self.aggregator = aggregator
            self.index_aggregator = index_aggregator
            self.kws = kws

            with self._history_lock:
                self._history_state = {
                    sym: HistoryState.HISTORY_LOADING
                    for sym in resolved_token_to_symbol.values()
                }
                self._history_epoch = {}

            live_market_state.reset()
            live_market_state.set_token_map(self.token_to_symbol)

        self._ensure_background_workers_started()

        ssf_one_minute_runtime.initialize(
            symbols=list(resolved_token_to_symbol.values()),
            kite_client=kite_client,
            seed_history=False,
        )

        self._ssf_seed_thread = threading.Thread(
            target=self._seed_ssf_history_background,
            args=(kite_client, generation),
            name="ssf-history-seed",
            daemon=True,
        )
        self._ssf_seed_thread.start()

        warmup_thread = threading.Thread(
            target=self._warm_historical_state,
            args=(dict(resolved_token_to_symbol), kite_client, generation),
            name="kite-history-warmup",
            daemon=True,
        )
        warmup_thread.start()

        kws.connect(threaded=True)

        logger.info(
            "[MarketStreamManager] KiteTicker stream connecting in background."
        )

        return {
            "status": "CONNECTING",
            "subscribed_tokens": len(tokens_to_subscribe),
            "symbols_count": len(resolved_token_to_symbol),
            "evaluation_workers": self._evaluation_worker_count,
            "evaluation_queue_size": self._evaluation_queue.qsize(),
        }

    def _stop_internal(self) -> None:
        self._stream_generation += 1
        self._last_accepted_tick_timestamp_by_token.clear()

        with self._history_lock:
            self._history_state.clear()
            self._history_epoch.clear()

        self._first_observed_candle.clear()
        self._awaiting_gap_recovery = False

        self._drain_evaluation_queue()
        self._drain_history_refresh_queue()
        self._drain_book_state()

        self._stop_event.set()
        for t in self._evaluation_worker_threads:
            if t.is_alive():
                t.join(timeout=0.5)
        self._evaluation_worker_threads.clear()
        self._evaluation_workers_started = False

        if self._history_refresh_thread and self._history_refresh_thread.is_alive():
            self._history_refresh_thread.join(timeout=0.5)
        self._history_refresh_thread = None
        self._history_refresh_worker_started = False

        if self._book_worker_thread and self._book_worker_thread.is_alive():
            self._book_worker_thread.join(timeout=0.5)
        self._book_worker_thread = None
        self._book_worker_started = False

        if self._flush_worker_thread and self._flush_worker_thread.is_alive():
            self._flush_worker_thread.join(timeout=0.5)
        self._flush_worker_thread = None
        self._flush_worker_started = False

        if self.kws is not None:
            try:
                self.kws.close()
            except Exception as exc:
                logger.warning(
                    "[MarketStreamManager] Error closing KiteTicker: %s",
                    exc,
                )
            self.kws = None

        latest_prices = {}
        try:
            latest_prices = {
                sym: float(st.ltp)
                for sym, st in live_market_state.get_all_symbols_state().items()
                if st and st.ltp and math.isfinite(st.ltp) and st.ltp > 0
            }
        except Exception:
            pass

        try:
            today_str = now_ist().strftime("%Y-%m-%d")
            db_manager.finalize_session_signals(
                trading_date=today_str,
                session_close_time=now_ist_naive(),
                current_prices=latest_prices,
            )
        except Exception as fin_exc:
            logger.debug(
                "[MarketStreamManager] Session finalization error: %s",
                fin_exc,
            )

        try:
            ssf_one_minute_runtime.stop()
        except Exception:
            logger.exception("[MarketStreamManager] Error stopping SSF 1m runtime")

        try:
            live_signal_engine.reset()
        except Exception:
            logger.exception(
                "[MarketStreamManager] Error resetting live signal engine"
            )

        try:
            live_market_state.reset()
        except Exception:
            logger.exception(
                "[MarketStreamManager] Error resetting live market state"
            )

        self.futures_token_to_symbol.clear()
        self.index_token_to_symbol.clear()
        self.token_to_symbol.clear()
        self.symbol_to_token.clear()
        self.subscribed_token_count = 0
        self.aggregator = None
        self.index_aggregator = None

        self.state = StreamState.STOPPED
        self.last_disconnect_time = now_ist()
        self._release_process_stream_lock()

    def stop_stream(self) -> Dict[str, Any]:
        with self._lock:
            self._stop_internal()
            return {
                "status": "STOPPED",
                "state": self.state.value,
            }

    def restart_stream(
        self,
        token_to_symbol: Optional[Dict[int, str]] = None,
        kite_client: Optional[Any] = None,
        timeframe_minutes: int = 15,
    ) -> Dict[str, Any]:
        self.stop_stream()
        return self.start_stream(
            token_to_symbol=token_to_symbol,
            kite_client=kite_client,
            timeframe_minutes=timeframe_minutes,
        )

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            is_connected = (
                self.state == StreamState.CONNECTED and self.kws is not None
            )

            last_tick_age_seconds = None

            if self.last_tick_time is not None:
                aware_last_tick = to_ist_aware(self.last_tick_time)
                if aware_last_tick is not None:
                    last_tick_age_seconds = (
                        (now_ist() - aware_last_tick).total_seconds()
                    )

            last_equity_tick_age_seconds = None

            if self.last_equity_tick_time is not None:
                aware_last_equity = to_ist_aware(self.last_equity_tick_time)
                if aware_last_equity is not None:
                    last_equity_tick_age_seconds = (
                        (now_ist() - aware_last_equity).total_seconds()
                    )

            with self._history_lock:
                history_counts = {state: 0 for state in HistoryState}
                for state in self._history_state.values():
                    history_counts[state] += 1

            with self._evaluation_queue_lock:
                pending_evaluation_count = len(self._pending_evaluations)

            return {
                "state": self.state.value,
                "connected": is_connected,
                "subscribed_token_count": self.subscribed_token_count,
                "tick_count": self.tick_count,
                "candle_count": self.candle_count,
                "last_tick_time": (
                    self.last_tick_time.isoformat()
                    if self.last_tick_time
                    else None
                ),
                "last_tick_age_seconds": (
                    round(last_tick_age_seconds, 3)
                    if last_tick_age_seconds is not None
                    else None
                ),
                "last_equity_tick_time": (
                    self.last_equity_tick_time.isoformat()
                    if self.last_equity_tick_time
                    else None
                ),
                "last_equity_tick_age_seconds": (
                    round(last_equity_tick_age_seconds, 3)
                    if last_equity_tick_age_seconds is not None
                    else None
                ),
                "last_connect_time": (
                    self.last_connect_time.isoformat()
                    if self.last_connect_time
                    else None
                ),
                "last_disconnect_time": (
                    self.last_disconnect_time.isoformat()
                    if self.last_disconnect_time
                    else None
                ),
                "last_error": self.last_error,
                "evaluation_workers": self._evaluation_worker_count,
                "evaluation_queue_size": self._evaluation_queue.qsize(),
                "evaluation_pending_count": pending_evaluation_count,
                "evaluation_inflight": self._evaluation_inflight,
                "evaluation_enqueued_count": self._evaluation_enqueued_count,
                "evaluation_coalesced_count": self._evaluation_coalesced_count,
                "evaluation_dropped_count": self._evaluation_dropped_count,
                "evaluation_stale_dropped_count": self._evaluation_stale_dropped_count,
                "last_evaluation_time": (
                    self._last_evaluation_time.isoformat()
                    if self._last_evaluation_time
                    else None
                ),
                "last_evaluation_error": self._last_evaluation_error,
                "history_ready_count": history_counts[HistoryState.HISTORY_READY],
                "history_loading_count": history_counts[HistoryState.HISTORY_LOADING],
                "history_stale_count": history_counts[HistoryState.HISTORY_STALE],
                "history_failed_count": history_counts[HistoryState.HISTORY_FAILED],
                "history_refresh_queue_size": self._history_refresh_queue.qsize(),
                "ticks_received_count": self._ticks_received_count,
                "ticks_accepted_count": self._ticks_accepted_count,
                "ticks_rejected_count": self._ticks_rejected_count,
                "ticks_invalid_count": self._ticks_invalid_count,
                "ticks_out_of_order_count": self._ticks_out_of_order_count,
                "l5_received_count": self._l5_received_count,
                "l5_processed_count": self._l5_processed_count,
                "l5_replaced_count": self._l5_replaced_count,
            }

    def get_volume_profile(
        self,
        symbol: str,
        current_price: Optional[float] = None,
    ) -> VolumeProfileFacts:
        sym = str(symbol).strip().upper()
        price = current_price
        if price is None or not math.isfinite(price) or price <= 0:
            st = live_market_state.get_symbol_state(sym)
            if st and st.ltp and math.isfinite(st.ltp) and st.ltp > 0:
                price = float(st.ltp)
        return self.volume_profile_engine.get_facts(
            symbol=sym,
            current_price=price,
        )


market_stream_manager = MarketStreamManager()