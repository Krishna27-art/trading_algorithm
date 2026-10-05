"""
Central WebSocket stream manager coordinating Zerodha KiteTicker,
MultiSymbolCandleAggregator, LiveMarketState, and LiveSignalEngine.

Production live-data flow
-------------------------
KiteTicker
    -> validated exchange-timestamped ticks
    -> live LTP/state update
    -> 15-minute candle aggregation
    -> bounded evaluation queue
    -> LiveSignalEngine
    -> PredictionService / strategies
    -> frontend
    -> manual execution by the user

This module NEVER places, changes, or cancels broker orders.

Runtime guarantees
------------------
- Strategy evaluation never runs inside the KiteTicker callback thread.
- Evaluation uses a bounded, coalescing queue.
- The first observed in-progress candle for a symbol is never evaluated.
- A skipped partial first candle is recovered from real Kite historical data.
- Fresh cash LTP is sent directly to LiveSignalEngine with exchange time.
- Kite cumulative volume is not passed as incremental tick volume.
- No fake instrument tokens or synthetic market data are created here.
"""

from __future__ import annotations

from enum import Enum
import logging
import os
import queue
import threading
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from broker.kite_adapter import get_saved_session, get_active_kite
from config.settings import settings
from config.universe import (
    StockUniverse,
    resolve_universe_tokens,
)
from data.candle_aggregator import MultiSymbolCandleAggregator
from data.historical_loader import HistoricalDataLoader
from data.instrument_resolver import instrument_resolver
from data.sector_peer_manager import get_sector_index_symbol
from data.time_utils import now_ist, now_ist_iso, now_ist_naive
from streaming.crsd_live_runtime import crsd_live_runtime
from streaming.live_market_state import live_market_state
from streaming.live_signal_engine import live_signal_engine
from streaming.ssf_market_context import ssf_context_store
from streaming.ssf_one_minute_runtime import ssf_one_minute_runtime


logger = logging.getLogger("streaming.market_stream_manager")


NSE_SESSION_OPEN = time(9, 15)
NSE_SESSION_CLOSE = time(15, 30)

DEFAULT_EVALUATION_WORKERS = 8
MAX_EVALUATION_WORKERS = 16

EVALUATION_QUEUE_SIZE = 1000
HISTORY_REFRESH_QUEUE_SIZE = 500
L5_QUEUE_SIZE = 5000


class StreamState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


class MarketStreamManager:
    """
    Singleton manager for the canonical KiteTicker market stream.

    One manager owns the production WebSocket stream.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # --------------------------------------------------------------
        # STREAM STATE
        # --------------------------------------------------------------
        self.state: StreamState = StreamState.DISCONNECTED

        self.token_to_symbol: Dict[int, str] = {}

        self.subscribed_token_count: int = 0

        self.tick_count: int = 0
        self.candle_count: int = 0

        # This is intentionally the server-side receipt timestamp.
        # It is used for feed freshness, not exchange candle timing.
        self.last_tick_time: Optional[datetime] = None

        self.last_connect_time: Optional[datetime] = None
        self.last_disconnect_time: Optional[datetime] = None

        self.last_error: Optional[str] = None

        self.kws: Optional[Any] = None
        self.aggregator: Optional[MultiSymbolCandleAggregator] = None

        self.futures_token_to_symbol: Dict[int, str] = {}
        self.index_token_to_symbol: Dict[int, str] = {}
        self.scanner_snapshot: Optional[Any] = None

        # --------------------------------------------------------------
        # HISTORY STATE
        # --------------------------------------------------------------
        self._history_ready_symbols: set[str] = set()
        self._history_lock = threading.Lock()

        # Historical cache/network operations are serialized so a warm-up
        # and a partial-candle recovery cannot write the same cache at once.
        self._history_io_lock = threading.Lock()

        # symbol -> (first observed candle bucket, first tick timestamp)
        #
        # Example:
        # stream starts at 11:07
        # first tick arrives at 11:07:30
        # bucket = 11:00
        #
        # The 11:00 candle is partial and must not be evaluated.
        self._first_observed_candle: Dict[
            str,
            Tuple[datetime, datetime],
        ] = {}

        # A stream generation invalidates old warm-up/history work after
        # a stop/restart.
        self._stream_generation: int = 0

        # --------------------------------------------------------------
        # EVALUATION WORKERS
        # --------------------------------------------------------------
        self._evaluation_worker_count = (
            self._read_worker_count()
        )

        # Queue contains symbols only.
        #
        # _pending_evaluations contains the latest task for that symbol.
        # This coalesces repeated candle evaluations and prevents an
        # unlimited backlog when strategy calculation is slower than input.
        self._evaluation_queue = queue.Queue(
            maxsize=EVALUATION_QUEUE_SIZE
        )

        self._evaluation_queue_lock = threading.Lock()

        self._pending_evaluations: Dict[
            str,
            Tuple[int, dict, float, Any],
        ] = {}

        self._evaluation_workers_started = False
        self._evaluation_worker_threads: List[
            threading.Thread
        ] = []

        self._evaluation_enqueued_count = 0
        self._evaluation_coalesced_count = 0
        self._evaluation_dropped_count = 0
        self._evaluation_inflight = 0

        self._last_evaluation_time: Optional[datetime] = None
        self._last_evaluation_error: Optional[str] = None

        # --------------------------------------------------------------
        # HISTORY REFRESH WORKER
        # --------------------------------------------------------------
        self._history_refresh_queue = queue.Queue(
            maxsize=HISTORY_REFRESH_QUEUE_SIZE
        )

        self._history_refresh_lock = threading.Lock()

        # symbol -> generation
        self._history_refresh_requested: Dict[
            str,
            int,
        ] = {}

        self._history_refresh_worker_started = False

        self._history_refresh_thread: Optional[
            threading.Thread
        ] = None

        # --------------------------------------------------------------
        # SSF L5 BOOK QUEUE AND WORKER
        # --------------------------------------------------------------
        self._book_queue = queue.Queue(
            maxsize=L5_QUEUE_SIZE
        )

        self._book_worker_started = False
        self._book_worker_thread: Optional[
            threading.Thread
        ] = None

        self._ssf_seed_thread: Optional[
            threading.Thread
        ] = None

    def set_scanner_snapshot(self, snapshot: Optional[Any]) -> None:
        with self._lock:
            self.scanner_snapshot = snapshot
        live_signal_engine.set_scanner_snapshot(snapshot)

    def get_scanner_snapshot(self) -> Optional[Any]:
        with self._lock:
            return self.scanner_snapshot

    # ==================================================================
    # CONFIGURATION
    # ==================================================================

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

        return max(
            1,
            min(
                value,
                MAX_EVALUATION_WORKERS,
            ),
        )

    def _ensure_background_workers_started(self) -> None:
        """
        Start permanent background workers once per backend process.
        """

        if not self._evaluation_workers_started:
            self._evaluation_workers_started = True

            for index in range(
                self._evaluation_worker_count
            ):
                thread = threading.Thread(
                    target=self._evaluation_worker_loop,
                    name=(
                        f"signal-evaluator-{index + 1}"
                    ),
                    daemon=True,
                )

                thread.start()

                self._evaluation_worker_threads.append(
                    thread
                )

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

    # ==================================================================
    # TIMESTAMP HELPERS
    # ==================================================================

    @staticmethod
    def _normalize_exchange_timestamp(
        value: Any,
    ) -> Optional[datetime]:
        """
        Normalize an exchange timestamp to naive Asia/Kolkata.

        Invalid/missing timestamps return None.

        IMPORTANT:
        Never replace invalid exchange time with server time.
        """

        if value is None:
            return None

        try:
            ts = pd.Timestamp(value)
        except (
            TypeError,
            ValueError,
        ):
            return None

        if pd.isna(ts):
            return None

        try:
            if ts.tzinfo is not None:
                ts = (
                    ts
                    .tz_convert("Asia/Kolkata")
                    .tz_localize(None)
                )
        except (
            TypeError,
            ValueError,
        ):
            return None

        return ts.to_pydatetime()

    @classmethod
    def _extract_exchange_timestamp(
        cls,
        tick: Dict[str, Any],
    ) -> Optional[datetime]:
        raw_timestamp = (
            tick.get("exchange_timestamp")
            or tick.get("timestamp")
        )

        return cls._normalize_exchange_timestamp(
            raw_timestamp
        )

    # ==================================================================
    # CANDLE BUCKET HELPERS
    # ==================================================================

    @staticmethod
    def _candle_bucket_start(
        timestamp: datetime,
        timeframe_minutes: int,
    ) -> Optional[datetime]:
        """
        Return the NSE session candle-open timestamp containing `timestamp`.

        Example for 15-minute candles:

            09:15 -> 09:15
            09:17 -> 09:15
            10:03 -> 10:00
            11:07 -> 11:00

        Timestamps outside the regular NSE session return None.
        """

        if timeframe_minutes <= 0:
            raise ValueError(
                "timeframe_minutes must be > 0"
            )

        session_open = datetime.combine(
            timestamp.date(),
            NSE_SESSION_OPEN,
        )

        session_close = datetime.combine(
            timestamp.date(),
            NSE_SESSION_CLOSE,
        )

        if timestamp < session_open:
            return None

        if timestamp >= session_close:
            return None

        elapsed_minutes = int(
            (
                timestamp - session_open
            ).total_seconds()
            // 60
        )

        bucket_offset = (
            elapsed_minutes
            // timeframe_minutes
        ) * timeframe_minutes

        return (
            session_open
            + timedelta(
                minutes=bucket_offset
            )
        )

    def _record_first_observed_candle(
        self,
        symbol: str,
        exchange_timestamp: datetime,
        timeframe_minutes: int,
    ) -> None:
        """
        Record the first candle bucket actually observed by this process.

        This is better than using only stream-start time because a symbol may
        receive its first tick several minutes after the WebSocket connects.
        """

        bucket_start = self._candle_bucket_start(
            exchange_timestamp,
            timeframe_minutes,
        )

        if bucket_start is None:
            return

        with self._lock:
            if symbol not in self._first_observed_candle:
                self._first_observed_candle[symbol] = (
                    bucket_start,
                    exchange_timestamp,
                )

    def _consume_partial_first_candle(
        self,
        symbol: str,
        candle_timestamp: datetime,
    ) -> bool:
        """
        Return True only when the candle is the symbol's first observed
        candle and its first tick arrived after the candle opened.
        """

        with self._lock:
            marker = self._first_observed_candle.pop(
                symbol,
                None,
            )

        if marker is None:
            return False

        bucket_start, first_tick_timestamp = marker

        return (
            candle_timestamp == bucket_start
            and first_tick_timestamp > bucket_start
        )

    # ==================================================================
    # HISTORY READINESS
    # ==================================================================

    def is_history_ready(
        self,
        symbol: str,
    ) -> bool:
        clean_symbol = str(
            symbol
        ).strip().upper()

        with self._history_lock:
            return (
                clean_symbol
                in self._history_ready_symbols
            )

    def _cache_path_for_symbol(
        self,
        symbol: str,
    ):
        return (
            settings.base_dir
            / "data"
            / "cache"
            / f"{symbol}_15m.csv"
        )

    # ==================================================================
    # HISTORY WARM-UP
    # ==================================================================

    def _warm_one_symbol_historical_state(
        self,
        token: int,
        symbol: str,
        kite_client: Any,
        generation: Optional[int] = None,
    ) -> bool:
        """
        Load real completed Kite history and seed LiveMarketState.

        `generation` prevents an old stream's background history request from
        mutating a newly-started stream after a restart.
        """

        clean_symbol = str(
            symbol
        ).strip().upper()

        cache_path = self._cache_path_for_symbol(
            clean_symbol
        )

        try:
            with self._history_io_lock:
                df = (
                    HistoricalDataLoader
                    .load_or_refresh_intraday_cache(
                        kite_client=kite_client,
                        instrument_token=int(token),
                        cache_path=cache_path,
                        now=now_ist_naive(),
                        lookback_days=45,
                        interval="15minute",
                    )
                )

            if generation is not None:
                with self._lock:
                    if (
                        generation
                        != self._stream_generation
                    ):
                        logger.debug(
                            "[MarketStreamManager] Ignoring stale "
                            "history result for %s from generation %s",
                            clean_symbol,
                            generation,
                        )
                        return False

            if df is None or df.empty:
                logger.warning(
                    "[MarketStreamManager] No historical 15m "
                    "data available for %s",
                    clean_symbol,
                )
                return False

            seeded = (
                live_market_state
                .seed_historical_candles(
                    symbol=clean_symbol,
                    candles=df,
                )
            )

            if seeded <= 0:
                logger.warning(
                    "[MarketStreamManager] Historical data for %s "
                    "contained no valid completed candles.",
                    clean_symbol,
                )
                return False

            with self._history_lock:
                self._history_ready_symbols.add(
                    clean_symbol
                )

            logger.info(
                "[MarketStreamManager] Seeded %s historical "
                "candles for %s",
                seeded,
                clean_symbol,
            )

            # Immediately enqueue initial strategy evaluation using the latest completed candle
            # so signals appear immediately on startup rather than forcing a 15-30 minute wait.
            try:
                latest_row = df.iloc[-1].to_dict()
                latest_candle = {
                    "symbol": clean_symbol,
                    "datetime": latest_row.get("datetime"),
                    "open": float(latest_row.get("open", 0.0)),
                    "high": float(latest_row.get("high", 0.0)),
                    "low": float(latest_row.get("low", 0.0)),
                    "close": float(latest_row.get("close", 0.0)),
                    "volume": int(latest_row.get("volume", 0)),
                }
                latest_vwap = float(latest_row.get("vwap", latest_candle["close"]))
                self._enqueue_evaluation(
                    candle_dict=latest_candle,
                    vwap=latest_vwap,
                    kite_client=kite_client,
                    generation=generation or self._stream_generation,
                )
            except Exception as eval_exc:
                logger.debug(
                    "[MarketStreamManager] Initial history evaluation enqueue skipped for %s: %s",
                    clean_symbol,
                    eval_exc,
                )

            return True

        except Exception as exc:
            logger.error(
                "[MarketStreamManager] Historical warm-up failed "
                "for %s token=%s: %s",
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
        """
        Warm every subscribed cash symbol with real completed history.

        No synthetic/fallback market data is produced.
        """

        for token, symbol in token_to_symbol.items():
            with self._lock:
                if (
                    generation
                    != self._stream_generation
                ):
                    return

            self._warm_one_symbol_historical_state(
                token=int(token),
                symbol=symbol,
                kite_client=kite_client,
                generation=generation,
            )

    def _book_worker_loop(self) -> None:
        while True:
            item = self._book_queue.get()

            try:
                symbol, snapshot = item

                live_signal_engine.on_book_update(
                    symbol=symbol,
                    snapshot=snapshot,
                )

            except Exception:
                logger.exception(
                    "[MarketStreamManager] SSF L5 worker failed."
                )

            finally:
                self._book_queue.task_done()

    def _seed_ssf_history_background(
        self,
        kite_client: Any,
        generation: int,
    ) -> None:
        try:
            with self._lock:
                if generation != self._stream_generation:
                    return

            ssf_one_minute_runtime.seed_historical_data(
                kite_client
            )

        except Exception:
            logger.exception(
                "[MarketStreamManager] Background SSF "
                "historical seeding failed."
            )

    # ==================================================================
    # PARTIAL-CANDLE HISTORY RECOVERY
    # ==================================================================

    def _schedule_symbol_history_refresh(
        self,
        symbol: str,
        token: Optional[int],
        kite_client: Any,
    ) -> bool:
        """
        Queue one real Kite historical refresh for a symbol.

        This is used when the first observed live candle was partial.
        """

        clean_symbol = str(
            symbol
        ).strip().upper()

        if token is None:
            logger.error(
                "[MarketStreamManager] Cannot refresh history for %s: "
                "missing real instrument token.",
                clean_symbol,
            )
            return False

        with self._lock:
            generation = self._stream_generation

        with self._history_refresh_lock:
            existing_generation = (
                self._history_refresh_requested.get(
                    clean_symbol
                )
            )

            if existing_generation == generation:
                return True

            self._history_refresh_requested[
                clean_symbol
            ] = generation

            try:
                self._history_refresh_queue.put_nowait(
                    (
                        generation,
                        clean_symbol,
                        int(token),
                        kite_client,
                    )
                )

                return True

            except queue.Full:
                self._history_refresh_requested.pop(
                    clean_symbol,
                    None,
                )

                logger.error(
                    "[MarketStreamManager] History refresh queue "
                    "full; cannot recover partial first candle for %s.",
                    clean_symbol,
                )

                return False

    def _history_refresh_worker_loop(
        self,
    ) -> None:
        """
        Single worker prevents hundreds of concurrent historical API calls.
        """

        while True:
            item = (
                self._history_refresh_queue.get()
            )

            generation = None
            symbol = None

            try:
                (
                    generation,
                    symbol,
                    token,
                    kite_client,
                ) = item

                self._warm_one_symbol_historical_state(
                    token=token,
                    symbol=symbol,
                    kite_client=kite_client,
                    generation=generation,
                )

            except Exception:
                logger.exception(
                    "[MarketStreamManager] Unhandled "
                    "history-refresh worker error"
                )

            finally:
                if symbol is not None:
                    with self._history_refresh_lock:
                        current = (
                            self._history_refresh_requested.get(
                                symbol
                            )
                        )

                        if current == generation:
                            self._history_refresh_requested.pop(
                                symbol,
                                None,
                            )

                self._history_refresh_queue.task_done()

    # ==================================================================
    # EVALUATION QUEUE
    # ==================================================================

    def _enqueue_evaluation(
        self,
        candle_dict: dict,
        vwap: float,
        kite_client: Any,
        generation: int,
    ) -> bool:
        """
        Add a completed candle to the bounded evaluation queue.

        One pending evaluation is maintained per symbol. If another candle
        arrives before the worker starts the old one, the old pending task is
        replaced by the newest candle.
        """

        symbol = str(
            candle_dict.get(
                "symbol",
                "",
            )
        ).strip().upper()

        if not symbol:
            logger.error(
                "[MarketStreamManager] Cannot queue candle "
                "without a symbol."
            )
            return False

        task = (
            generation,
            dict(candle_dict),
            float(vwap),
            kite_client,
        )

        with self._evaluation_queue_lock:

            already_pending = (
                symbol
                in self._pending_evaluations
            )

            # Always retain the newest task for the symbol.
            self._pending_evaluations[
                symbol
            ] = task

            if already_pending:
                self._evaluation_coalesced_count += 1
                return True

            try:
                self._evaluation_queue.put_nowait(
                    symbol
                )

                self._evaluation_enqueued_count += 1

                return True

            except queue.Full:

                # The queue is bounded. Preserve the newest symbol by
                # evicting one older queued symbol instead of blocking the
                # KiteTicker callback thread.
                try:
                    dropped_symbol = (
                        self._evaluation_queue.get_nowait()
                    )

                    self._pending_evaluations.pop(
                        dropped_symbol,
                        None,
                    )

                    self._evaluation_queue.task_done()

                except queue.Empty:
                    dropped_symbol = None

                try:
                    self._evaluation_queue.put_nowait(
                        symbol
                    )

                    self._evaluation_enqueued_count += 1
                    self._evaluation_dropped_count += 1

                    logger.error(
                        "[MarketStreamManager] Evaluation queue full; "
                        "dropped pending symbol=%s to preserve latest symbol=%s",
                        dropped_symbol,
                        symbol,
                    )

                    return True

                except queue.Full:
                    self._pending_evaluations.pop(
                        symbol,
                        None,
                    )

                    self._evaluation_dropped_count += 1

                    logger.error(
                        "[MarketStreamManager] Evaluation queue full; "
                        "dropped latest candle for %s.",
                        symbol,
                    )

                    return False

    def _evaluation_worker_loop(
        self,
    ) -> None:
        """
        Background strategy evaluation worker.

        IMPORTANT:
        This function is never called from KiteTicker.on_ticks().
        """

        while True:
            symbol = (
                self._evaluation_queue.get()
            )

            try:
                with self._evaluation_queue_lock:
                    task = (
                        self._pending_evaluations.pop(
                            symbol,
                            None,
                        )
                    )

                if task is None:
                    continue

                (
                    generation,
                    candle_dict,
                    vwap,
                    kite_client,
                ) = task

                with self._lock:
                    current_generation = (
                        self._stream_generation
                    )
                    self._evaluation_inflight += 1

                if (
                    generation
                    != current_generation
                ):
                    continue

                try:
                    live_signal_engine.on_candle_close(
                        candle_dict,
                        vwap,
                        kite_client=kite_client,
                    )

                    with self._lock:
                        self._last_evaluation_time = (
                            now_ist()
                        )
                        self._last_evaluation_error = None

                except Exception as exc:
                    error = (
                        f"{type(exc).__name__}: {exc}"
                    )

                    with self._lock:
                        self._last_evaluation_error = (
                            f"{symbol}: {error}"
                        )

                    logger.exception(
                        "[MarketStreamManager] Strategy evaluation "
                        "failed for %s",
                        symbol,
                    )

                finally:
                    with self._lock:
                        self._evaluation_inflight = max(
                            0,
                            self._evaluation_inflight - 1,
                        )

            except Exception:
                logger.exception(
                    "[MarketStreamManager] Unhandled "
                    "evaluation-worker error"
                )

            finally:
                self._evaluation_queue.task_done()

    def _drain_evaluation_queue(
        self,
    ) -> None:
        """
        Remove queued evaluation work.

        In-flight evaluations cannot be interrupted safely and are protected
        by stream generation checks before starting.
        """

        with self._evaluation_queue_lock:
            self._pending_evaluations.clear()

            while True:
                try:
                    self._evaluation_queue.get_nowait()
                    self._evaluation_queue.task_done()
                except queue.Empty:
                    break

    def _drain_history_refresh_queue(
        self,
    ) -> None:
        with self._history_refresh_lock:
            self._history_refresh_requested.clear()

            while True:
                try:
                    self._history_refresh_queue.get_nowait()
                    self._history_refresh_queue.task_done()
                except queue.Empty:
                    break

    # ==================================================================
    # STREAM START
    # ==================================================================

    def start_stream(
        self,
        token_to_symbol: Optional[
            Dict[int, str]
        ] = None,
        kite_client: Optional[Any] = None,
        timeframe_minutes: int = 15,
    ) -> Dict[str, Any]:
        """
        Start the single canonical KiteTicker stream.

        No strategy calculation occurs synchronously inside the KiteTicker
        callback thread.
        """

        if timeframe_minutes <= 0:
            raise ValueError(
                "timeframe_minutes must be > 0"
            )

        with self._lock:

            # ----------------------------------------------------------
            # Stop an existing WebSocket before starting a new one.
            # ----------------------------------------------------------
            if self.kws is not None:
                self._stop_internal()

            self._stream_generation += 1

            generation = (
                self._stream_generation
            )

            self._first_observed_candle.clear()

            self._drain_evaluation_queue()

            self._drain_history_refresh_queue()

            # ----------------------------------------------------------
            # 1. Resolve authenticated Kite client.
            # ----------------------------------------------------------
            if kite_client is None:
                kite_client = get_active_kite()

            if kite_client is None:
                self.state = StreamState.ERROR

                self.last_error = (
                    "No active Zerodha Kite client available. "
                    "Please authenticate via Kite Connect."
                )

                raise RuntimeError(
                    self.last_error
                )

            # ----------------------------------------------------------
            # 2. Resolve real universe tokens.
            # ----------------------------------------------------------
            if not token_to_symbol:

                token_map = (
                    resolve_universe_tokens(
                        kite_client=kite_client
                    )
                )

                token_to_symbol = {
                    int(token): (
                        str(symbol)
                        .strip()
                        .upper()
                    )
                    for symbol, token
                    in token_map.items()
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

                missing_symbols = (
                    expected_symbols
                    - resolved_symbols
                )

                unexpected_symbols = (
                    resolved_symbols
                    - expected_symbols
                )

                if (
                    missing_symbols
                    or unexpected_symbols
                    or len(resolved_symbols) != len(expected_symbols)
                ):
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

                    logger.error(
                        "[MarketStreamManager] %s",
                        self.last_error,
                    )

                    raise RuntimeError(
                        self.last_error
                    )

            else:
                token_to_symbol = {
                    int(token): (
                        str(symbol)
                        .strip()
                        .upper()
                    )
                    for token, symbol
                    in token_to_symbol.items()
                    if token and symbol
                }

            if not token_to_symbol:
                self.state = StreamState.ERROR

                self.last_error = (
                    "No valid instrument tokens available "
                    "to start market stream."
                )

                raise ValueError(
                    self.last_error
                )

            # ----------------------------------------------------------
            # 3. Validate saved Kite session.
            # ----------------------------------------------------------
            session = get_saved_session()

            if not session:
                self.state = StreamState.ERROR

                self.last_error = (
                    "No active Zerodha Kite session found. "
                    "Please authenticate via Kite login."
                )

                raise RuntimeError(
                    self.last_error
                )

            api_key = session.get(
                "api_key"
            )

            access_token = session.get(
                "access_token"
            )

            if not api_key or not access_token:
                self.state = StreamState.ERROR
                self.last_error = (
                    "Invalid session credentials."
                )

                raise RuntimeError(
                    self.last_error
                )

            self.token_to_symbol = dict(
                token_to_symbol
            )

            # ----------------------------------------------------------
            # 4. Resolve real futures tokens.
            # ----------------------------------------------------------
            self.futures_token_to_symbol.clear()

            for sym in (
                self.token_to_symbol.values()
            ):
                try:
                    fut_info = (
                        instrument_resolver
                        .find_nearest_single_stock_future(
                            sym,
                            kite_client=kite_client,
                        )
                    )

                    if fut_info is None:
                        continue

                    raw_fut_token = fut_info.get(
                        "instrument_token"
                    )

                    try:
                        fut_token = int(
                            raw_fut_token
                        )
                    except (TypeError, ValueError):
                        logger.warning(
                            "[MarketStreamManager] Invalid futures "
                            "instrument token for %s: %r",
                            sym,
                            raw_fut_token,
                        )
                        continue

                    if fut_token <= 0:
                        logger.warning(
                            "[MarketStreamManager] Non-positive futures "
                            "instrument token for %s: %s",
                            sym,
                            fut_token,
                        )
                        continue

                    self.futures_token_to_symbol[
                        fut_token
                    ] = sym

                except Exception as exc:
                    logger.debug(
                        "[MarketStreamManager] Failed to resolve "
                        "futures token for %s: %s",
                        sym,
                        exc,
                    )

            # ----------------------------------------------------------
            # 5. Resolve real sector-index tokens.
            # ----------------------------------------------------------
            self.index_token_to_symbol.clear()

            for sym in (
                self.token_to_symbol.values()
            ):
                try:
                    idx_sym = (
                        get_sector_index_symbol(
                            sym
                        )
                    )

                    if not idx_sym:
                        continue

                    idx_tok = (
                        instrument_resolver
                        .resolve_token(
                            idx_sym,
                            exchange="NSE",
                            kite_client=kite_client,
                        )
                    )

                    if idx_tok:
                        self.index_token_to_symbol[
                            int(idx_tok)
                        ] = idx_sym

                except Exception as exc:
                    logger.debug(
                        "[MarketStreamManager] Failed to resolve "
                        "index token for %s: %s",
                        sym,
                        exc,
                    )

            # Explicitly resolve and subscribe real NIFTY benchmark token for CRSD market factor
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
                    self.index_token_to_symbol[int(nifty_tok)] = "NIFTY"
                    self.token_to_symbol[int(nifty_tok)] = "NIFTY"
            except Exception as exc:
                logger.debug(
                    "[MarketStreamManager] Failed to resolve NIFTY token: %s",
                    exc,
                )

            # ----------------------------------------------------------
            # 6. Deduplicate actual subscription tokens.
            # ----------------------------------------------------------
            all_tokens = list(
                dict.fromkeys(
                    list(
                        self.token_to_symbol.keys()
                    )
                    + list(
                        self.futures_token_to_symbol.keys()
                    )
                    + list(
                        self.index_token_to_symbol.keys()
                    )
                )
            )

            self.subscribed_token_count = (
                len(all_tokens)
            )

            self.state = (
                StreamState.CONNECTING
            )

            self.last_error = None

            # ----------------------------------------------------------
            # 7. Reset live state for this stream.
            # ----------------------------------------------------------
            live_market_state.reset()

            live_market_state.set_token_map(
                self.token_to_symbol
            )

            # ----------------------------------------------------------
            # 8. Start evaluation/history workers.
            # ----------------------------------------------------------
            self._ensure_background_workers_started()

            # ----------------------------------------------------------
            # 9. Initialize SSF one-minute runtime.
            # ----------------------------------------------------------
            ssf_one_minute_runtime.initialize(
                symbols=list(
                    self.token_to_symbol.values()
                ),
                kite_client=kite_client,
                seed_history=False,
            )

            self._ssf_seed_thread = threading.Thread(
                target=self._seed_ssf_history_background,
                args=(
                    kite_client,
                    self._stream_generation,
                ),
                name="ssf-history-seed",
                daemon=True,
            )

            self._ssf_seed_thread.start()

            # ==========================================================
            # CANDLE CLOSE CALLBACK
            # ==========================================================

            def _on_candle_close(
                candle_dict: dict,
                vwap: float,
            ) -> None:

                symbol = str(
                    candle_dict.get(
                        "symbol",
                        "",
                    )
                ).strip().upper()

                candle_timestamp = (
                    self._normalize_exchange_timestamp(
                        candle_dict.get(
                            "datetime"
                        )
                    )
                )

                if (
                    not symbol
                    or candle_timestamp is None
                ):
                    logger.error(
                        "[MarketStreamManager] Rejected malformed "
                        "candle-close callback: %r",
                        candle_dict,
                    )
                    return

                with self._lock:
                    self.candle_count += 1
                    current_generation = (
                        self._stream_generation
                    )

                # ------------------------------------------------------
                # FIRST CANDLE PARTIAL-CANDLE PROTECTION
                # ------------------------------------------------------
                if self._consume_partial_first_candle(
                    symbol=symbol,
                    candle_timestamp=candle_timestamp,
                ):
                    logger.warning(
                        "[MarketStreamManager] Ignoring partial first "
                        "candle for %s at %s. Scheduling real Kite "
                        "historical backfill.",
                        symbol,
                        candle_timestamp,
                    )

                    state = (
                        live_market_state
                        .get_symbol_state(
                            symbol
                        )
                    )

                    token = (
                        (state.token if state is not None else None)
                        or self.symbol_to_token.get(symbol)
                    )

                    self._schedule_symbol_history_refresh(
                        symbol=symbol,
                        token=token,
                        kite_client=kite_client,
                    )

                    return

                # ------------------------------------------------------
                # Only complete valid candles reach strategy state.
                # ------------------------------------------------------
                live_market_state.update_candle_close(
                    candle_dict,
                    vwap,
                )

                if symbol in ("NIFTY", "NIFTY 50", "NIFTY50", "__MARKET__"):
                    crsd_live_runtime.update_market_candle(
                        candle_dict
                    )
                else:
                    crsd_live_runtime.update_stock_candle(
                        candle_dict
                    )

                # ------------------------------------------------------
                # History must be ready before strategy evaluation.
                # ------------------------------------------------------
                if not self.is_history_ready(
                    symbol
                ):
                    logger.info(
                        "[MarketStreamManager] Skipping strategy "
                        "evaluation for %s: historical warm-up "
                        "not complete.",
                        symbol,
                    )
                    return

                # ------------------------------------------------------
                # NEVER execute strategy calculation here.
                #
                # Queue it for worker threads so the KiteTicker callback
                # returns immediately.
                # ------------------------------------------------------
                queued = (
                    self._enqueue_evaluation(
                        candle_dict=candle_dict,
                        vwap=vwap,
                        kite_client=kite_client,
                        generation=current_generation,
                    )
                )

                if not queued:
                    logger.error(
                        "[MarketStreamManager] Could not queue completed "
                        "candle evaluation for %s.",
                        symbol,
                    )

            # ==========================================================
            # BOOK UPDATE CALLBACK
            # ==========================================================

            def _on_book_update(
                symbol: str,
                snapshot: Any,
            ) -> None:

                live_market_state.update_book_snapshot(
                    symbol,
                    snapshot,
                )

                try:
                    self._book_queue.put_nowait(
                        (
                            symbol,
                            snapshot,
                        )
                    )
                except queue.Full:
                    logger.error(
                        "[MarketStreamManager] SSF L5 queue full; "
                        "dropping newest snapshot for %s.",
                        symbol,
                    )

            # ==========================================================
            # CANDLE AGGREGATOR
            # ==========================================================

            self.aggregator = (
                MultiSymbolCandleAggregator(
                    token_to_symbol_map=(
                        self.token_to_symbol
                    ),
                    timeframe_minutes=(
                        timeframe_minutes
                    ),
                    on_candle_close=(
                        _on_candle_close
                    ),
                    on_book_update=(
                        _on_book_update
                    ),
                )
            )

            # ==========================================================
            # KITE TICKER
            # ==========================================================

            try:
                from kiteconnect import (
                    KiteTicker,
                )
            except ImportError:
                self.state = StreamState.ERROR

                self.last_error = (
                    "kiteconnect package is not installed."
                )

                raise RuntimeError(
                    self.last_error
                )

            kws = KiteTicker(
                api_key,
                access_token,
            )

            tokens_to_subscribe = list(
                all_tokens
            )

            # ==========================================================
            # ON TICKS
            # ==========================================================

            def on_ticks(
                ws,
                ticks,
            ):
                """
                Keep this callback lightweight.

                It validates/dispatches data and returns.
                Heavy strategy evaluation is NEVER performed here.
                """

                with self._lock:
                    self.tick_count += (
                        len(ticks)
                        if isinstance(
                            ticks,
                            list,
                        )
                        else 1
                    )

                    # Receipt time for feed-freshness monitoring.
                    self.last_tick_time = (
                        now_ist()
                    )

                tick_batch = (
                    ticks
                    if isinstance(
                        ticks,
                        list,
                    )
                    else [ticks]
                )

                cash_ticks: List[
                    dict
                ] = []

                for tick in tick_batch:

                    if not isinstance(
                        tick,
                        dict,
                    ):
                        continue

                    raw_token = tick.get(
                        "instrument_token"
                    )

                    try:
                        tok = int(
                            raw_token
                        )
                    except (
                        TypeError,
                        ValueError,
                    ):
                        continue

                    exchange_timestamp = (
                        self._extract_exchange_timestamp(
                            tick
                        )
                    )

                    # ==================================================
                    # FUTURES TICK
                    # ==================================================

                    if (
                        tok
                        in self.futures_token_to_symbol
                    ):
                        fut_sym = (
                            self
                            .futures_token_to_symbol[
                                tok
                            ]
                        )

                        fut_ltp = tick.get(
                            "last_price"
                        )

                        fut_oi = tick.get(
                            "oi"
                        )

                        if (
                            exchange_timestamp
                            is not None
                        ):
                            ssf_context_store.update_futures(
                                symbol=fut_sym,
                                fut_ltp=fut_ltp,
                                fut_oi=fut_oi,
                                timestamp=(
                                    exchange_timestamp
                                ),
                            )

                        else:
                            logger.debug(
                                "[MarketStreamManager] Ignoring "
                                "futures context update because "
                                "exchange timestamp is missing. "
                                "token=%s",
                                tok,
                            )

                        continue

                    # ==================================================
                    # SECTOR INDEX TICK
                    # ==================================================

                    if (
                        tok
                        in self.index_token_to_symbol
                    ):
                        # ssf_one_minute_runtime performs its own
                        # validation. Do not invent a timestamp here.
                        if (
                            exchange_timestamp
                            is not None
                        ):
                            ssf_one_minute_runtime.on_tick(
                                tick
                            )

                        # NIFTY is also intentionally part of
                        # token_to_symbol so it must enter the
                        # normal 15-minute aggregation pipeline.
                        if tok in self.token_to_symbol:
                            cash_ticks.append(tick)

                        continue

                    # ==================================================
                    # CASH STOCK TICK
                    # ==================================================

                    sym = (
                        self.token_to_symbol.get(
                            tok
                        )
                    )

                    last_price = (
                        tick.get(
                            "last_price"
                        )
                    )

                    if (
                        sym is None
                        or last_price is None
                        or exchange_timestamp is None
                    ):
                        continue

                    try:
                        price = float(
                            last_price
                        )
                    except (
                        TypeError,
                        ValueError,
                    ):
                        continue

                    if price <= 0:
                        continue

                    # --------------------------------------------------
                    # Detect partial first candle.
                    # Must happen BEFORE aggregator.process_ticks().
                    # --------------------------------------------------
                    self._record_first_observed_candle(
                        symbol=sym,
                        exchange_timestamp=(
                            exchange_timestamp
                        ),
                        timeframe_minutes=(
                            timeframe_minutes
                        ),
                    )

                    # --------------------------------------------------
                    # FIX: record the freshest REAL cash LTP directly
                    # into LiveSignalEngine.
                    # --------------------------------------------------
                    #
                    # IMPORTANT:
                    # Use the exchange timestamp.
                    # Do NOT replace it with now_ist().
                    #
                    live_signal_engine.record_tick_price(
                        symbol=sym,
                        price=price,
                        timestamp=(
                            exchange_timestamp
                        ),
                    )

                    # --------------------------------------------------
                    # LiveMarketState expects incremental volume.
                    #
                    # Kite volume_traded is cumulative session volume.
                    # CandleAggregator already converts cumulative volume
                    # into incremental candle volume.
                    #
                    # Therefore we intentionally pass ZERO here.
                    # This prevents cumulative-volume double counting.
                    # --------------------------------------------------
                    live_market_state.update_tick(
                        symbol=sym,
                        price=price,
                        volume=0,
                        timestamp=(
                            exchange_timestamp
                        ),
                        token=tok,
                    )

                    # --------------------------------------------------
                    # Real cash tick also feeds SSF runtime.
                    # --------------------------------------------------
                    ssf_one_minute_runtime.on_tick(
                        tick
                    )

                    cash_ticks.append(
                        tick
                    )

                # ======================================================
                # CANDLE AGGREGATION
                # ======================================================

                if (
                    self.aggregator
                    and cash_ticks
                ):
                    try:
                        self.aggregator.process_ticks(
                            cash_ticks
                        )

                    except Exception:
                        logger.exception(
                            "[MarketStreamManager] Error processing "
                            "cash ticks in CandleAggregator"
                        )

            # ==========================================================
            # CONNECTION CALLBACKS
            # ==========================================================

            def on_connect(
                ws,
                response,
            ):
                with self._lock:
                    self.state = (
                        StreamState.CONNECTED
                    )

                    self.last_connect_time = (
                        now_ist()
                    )

                    self.last_error = None

                ws.subscribe(
                    tokens_to_subscribe
                )

                ws.set_mode(
                    ws.MODE_FULL,
                    tokens_to_subscribe,
                )

                logger.info(
                    "[MarketStreamManager] KiteTicker connected "
                    "in MODE_FULL — subscribed %d instruments "
                    "(%d cash, %d futures, %d indices).",
                    len(
                        tokens_to_subscribe
                    ),
                    len(
                        self.token_to_symbol
                    ),
                    len(
                        self.futures_token_to_symbol
                    ),
                    len(
                        self.index_token_to_symbol
                    ),
                )

            def on_close(
                ws,
                code,
                reason,
            ):
                with self._lock:
                    self.state = (
                        StreamState.DISCONNECTED
                    )

                    self.last_disconnect_time = (
                        now_ist()
                    )

                logger.warning(
                    "[MarketStreamManager] KiteTicker closed: "
                    "code=%s reason=%s",
                    code,
                    reason,
                )

            def on_error(
                ws,
                code,
                reason,
            ):
                with self._lock:
                    self.state = (
                        StreamState.ERROR
                    )

                    self.last_error = (
                        f"code={code} reason={reason}"
                    )

                logger.error(
                    "[MarketStreamManager] KiteTicker error: %s",
                    self.last_error,
                )

            def on_reconnect(
                ws,
                attempts_count,
            ):
                with self._lock:
                    self.state = (
                        StreamState.RECONNECTING
                    )

                logger.info(
                    "[MarketStreamManager] KiteTicker reconnecting "
                    "(attempt %s)...",
                    attempts_count,
                )

            # ==========================================================
            # REGISTER CALLBACKS
            # ==========================================================

            kws.on_ticks = on_ticks
            kws.on_connect = on_connect
            kws.on_close = on_close
            kws.on_error = on_error
            kws.on_reconnect = on_reconnect

            self.kws = kws

            # ==========================================================
            # HISTORICAL WARM-UP
            # ==========================================================

            warmup_thread = threading.Thread(
                target=self._warm_historical_state,
                args=(
                    dict(
                        self.token_to_symbol
                    ),
                    kite_client,
                    generation,
                ),
                name="kite-history-warmup",
                daemon=True,
            )

            warmup_thread.start()

            # ==========================================================
            # CONNECT
            # ==========================================================

            kws.connect(
                threaded=True
            )

            logger.info(
                "[MarketStreamManager] KiteTicker stream "
                "connecting in background."
            )

            return {
                "status": "CONNECTING",
                "subscribed_tokens": len(
                    tokens_to_subscribe
                ),
                "symbols_count": len(
                    self.token_to_symbol
                ),
                "evaluation_workers": (
                    self._evaluation_worker_count
                ),
                "evaluation_queue_size": (
                    self._evaluation_queue.qsize()
                ),
            }

    # ==================================================================
    # STOP
    # ==================================================================

    def _stop_internal(self) -> None:
        """
        Stop the current WebSocket generation.

        Caller must hold self._lock.
        """

        # Invalidate queued/background work belonging to this stream.
        self._stream_generation += 1

        with self._history_lock:
            self._history_ready_symbols.clear()

        self._first_observed_candle.clear()

        self._drain_evaluation_queue()

        self._drain_history_refresh_queue()

        # Stop SSF runtime safely.
        try:
            ssf_one_minute_runtime.stop()
        except Exception:
            logger.exception(
                "[MarketStreamManager] Error stopping "
                "SSF 1m runtime"
            )

        # Reset signal engine state.
        try:
            live_signal_engine.reset()
        except Exception:
            logger.exception(
                "[MarketStreamManager] Error resetting "
                "live signal engine"
            )

        # Reset market state.
        try:
            live_market_state.reset()
        except Exception:
            logger.exception(
                "[MarketStreamManager] Error resetting "
                "live market state"
            )

        self.futures_token_to_symbol.clear()
        self.index_token_to_symbol.clear()

        self.token_to_symbol.clear()

        self.subscribed_token_count = 0

        self.aggregator = None

        # Close WebSocket.
        if self.kws is not None:
            try:
                self.kws.close()
            except Exception as exc:
                logger.warning(
                    "[MarketStreamManager] Error closing KiteTicker: %s",
                    exc,
                )

            self.kws = None

        self.state = (
            StreamState.STOPPED
        )

        self.last_disconnect_time = (
            now_ist()
        )

    def stop_stream(
        self,
    ) -> Dict[str, Any]:

        with self._lock:
            self._stop_internal()

            return {
                "status": "STOPPED",
                "state": self.state.value,
            }

    # ==================================================================
    # STATUS
    # ==================================================================

    def get_status(
        self,
    ) -> Dict[str, Any]:

        with self._lock:

            is_connected = (
                self.state
                == StreamState.CONNECTED
                and self.kws is not None
            )

            last_tick_age_seconds = None

            if self.last_tick_time is not None:

                last_tick_age_seconds = max(
                    0.0,
                    (
                        now_ist()
                        - self.last_tick_time
                    ).total_seconds(),
                )

            return {
                "state": (
                    self.state.value
                ),

                "connected": (
                    is_connected
                ),

                "subscribed_token_count": (
                    self.subscribed_token_count
                ),

                "tick_count": (
                    self.tick_count
                ),

                "candle_count": (
                    self.candle_count
                ),

                "last_tick_time": (
                    self.last_tick_time.isoformat()
                    if self.last_tick_time
                    else None
                ),

                "last_tick_age_seconds": (
                    round(
                        last_tick_age_seconds,
                        3,
                    )
                    if last_tick_age_seconds is not None
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

                "last_error": (
                    self.last_error
                ),

                "evaluation_workers": (
                    self._evaluation_worker_count
                ),

                "evaluation_queue_size": (
                    self._evaluation_queue.qsize()
                ),

                "evaluation_inflight": (
                    self._evaluation_inflight
                ),

                "evaluation_enqueued_count": (
                    self._evaluation_enqueued_count
                ),

                "evaluation_coalesced_count": (
                    self._evaluation_coalesced_count
                ),

                "evaluation_dropped_count": (
                    self._evaluation_dropped_count
                ),

                "last_evaluation_time": (
                    self._last_evaluation_time.isoformat()
                    if self._last_evaluation_time
                    else None
                ),

                "last_evaluation_error": (
                    self._last_evaluation_error
                ),

                "history_ready_count": len(self._history_ready_symbols),
                "history_refresh_queue_size": self._history_refresh_queue.qsize(),
            }


market_stream_manager = (
    MarketStreamManager()
)