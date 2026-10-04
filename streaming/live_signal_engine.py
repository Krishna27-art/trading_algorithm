"""
Live signal computation engine driven by completed streaming candles and
real-time Level-5 market state.

Design contract
---------------
- Completed candles are the ONLY candle input to strategy evaluation.
- The forming candle is never evaluated.
- Current LTP is kept separate from completed-candle close.
- Latest Level-5 snapshot LTP is preferred because the stream already carries
  the real latest traded price.
- A candle close never overwrites the concept of current market price.
- Duplicate candle-close events are ignored.
- Strategy failures are isolated: one broken strategy cannot invalidate the
  remaining strategies or silently disappear.
- Consensus is produced only from PredictionService's validated result.
- All timestamps exposed by this module use Asia/Kolkata.

Data structures
---------------
_predictions:
    symbol -> latest prediction dictionary
    O(1) average lookup/update

_processed_candle_keys:
    set[(symbol, candle_open_timestamp)]
    O(1) average duplicate detection

latest_ltp:
    symbol -> latest verified price
    O(1) average lookup/update
"""

from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timedelta
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import pandas as pd

from data.time_utils import now_ist, now_ist_iso
from strategy.prediction_service import prediction_service
from strategy.ssf_l5_srm_strategy import BookSnapshot
from streaming.live_market_state import live_market_state
from streaming.ssf_live_runtime import ssf_live_runtime
from streaming.ssf_market_context import ssf_context_store


logger = logging.getLogger("streaming.live_signal_engine")


_VALID_DIRECTIONS = {"LONG", "SHORT"}
_ERROR_STATUSES = {"ERROR", "UNAVAILABLE"}


class LiveSignalEngine:
    """
    Maintains the latest real-time strategy signal for every subscribed symbol.

    The engine is intentionally read-only with respect to broker execution.
    It creates signals only. It never places, changes, or cancels orders.
    """

    def __init__(
        self,
        max_processed_keys: int = 10000,
        max_ltp_age_seconds: int = 120,
    ) -> None:
        if max_processed_keys <= 0:
            raise ValueError("max_processed_keys must be > 0")
        if max_ltp_age_seconds <= 0:
            raise ValueError("max_ltp_age_seconds must be > 0")

        self._lock = threading.RLock()

        self._predictions: Dict[str, Dict[str, Any]] = {}
        self._latest_ltp: Dict[str, Tuple[float, datetime]] = {}
        self._processed_candle_keys: set[Tuple[str, datetime]] = set()
        self._processed_candle_order: Deque[Tuple[str, datetime]] = deque()

        self._max_processed_keys = int(max_processed_keys)
        self._max_ltp_age_seconds = int(max_ltp_age_seconds)

        # Shared persistent SSF strategy registry — one SsfL5SrmStrategy per symbol
        # survives across candle-close and book-update events so that rolling
        # z-score buffers, basis history, and regime state accumulate correctly.
        self._ssf_runtime = ssf_live_runtime

    # ------------------------------------------------------------------
    # NORMALIZATION / VALIDATION
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_symbol(symbol: Any) -> Optional[str]:
        if symbol is None:
            return None

        normalized = str(symbol).strip().upper()
        return normalized or None

    @staticmethod
    def _normalize_timestamp(value: Any) -> Optional[datetime]:
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

    @staticmethod
    def _valid_price(value: Any) -> bool:
        try:
            price = float(value)
        except (TypeError, ValueError):
            return False

        return math.isfinite(price) and price > 0.0

    @staticmethod
    def _copy_dataframe(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()

        data = df.copy()

        if "datetime" not in data.columns:
            return pd.DataFrame()

        try:
            data["datetime"] = pd.to_datetime(data["datetime"])
        except (TypeError, ValueError):
            return pd.DataFrame()

        try:
            if getattr(data["datetime"].dt, "tz", None) is not None:
                data["datetime"] = (
                    data["datetime"]
                    .dt.tz_convert("Asia/Kolkata")
                    .dt.tz_localize(None)
                )
        except (TypeError, ValueError):
            return pd.DataFrame()

        data = data.dropna(subset=["datetime"])
        data = data.sort_values("datetime")

        # A completed candle must appear only once.
        data = data.drop_duplicates(
            subset=["datetime"],
            keep="last",
        )

        return data.reset_index(drop=True)

    def _validate_candle_contract(
        self,
        candle_dict: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[datetime]]:
        if not isinstance(candle_dict, dict):
            return None, None

        symbol = self._normalize_symbol(
            candle_dict.get("symbol")
        )

        if symbol is None:
            return None, None

        timestamp = self._normalize_timestamp(
            candle_dict.get("datetime")
        )

        if timestamp is None:
            return None, None

        required = (
            "open",
            "high",
            "low",
            "close",
        )

        for field in required:
            if not self._valid_price(
                candle_dict.get(field)
            ):
                return None, None

        try:
            high = float(candle_dict["high"])
            low = float(candle_dict["low"])
            open_price = float(candle_dict["open"])
            close = float(candle_dict["close"])
        except (TypeError, ValueError):
            return None, None

        if low > high:
            return None, None

        if not (
            low <= open_price <= high
            and low <= close <= high
        ):
            return None, None

        return symbol, timestamp

    # ------------------------------------------------------------------
    # CURRENT LTP
    # ------------------------------------------------------------------

    def _read_live_ltp(
        self,
        symbol: str,
    ) -> Tuple[
        Optional[float],
        str,
        Optional[datetime],
    ]:
        """
        Resolve only a FRESH real market LTP.

        Allowed sources:
            1. Fresh Level-5 snapshot LTP.
            2. Fresh engine-captured tick LTP.

        A completed candle close is NEVER used as current_ltp.
        """
        state = live_market_state.get_symbol_state(
            symbol
        )

        now = now_ist().replace(
            tzinfo=None
        )

        # --------------------------------------------------------------
        # 1. Fresh Level-5 snapshot
        # --------------------------------------------------------------
        if (
            state is not None
            and state.book_snapshot is not None
        ):
            snapshot = state.book_snapshot

            snapshot_ltp = getattr(
                snapshot,
                "ltp",
                None,
            )

            snapshot_ts = self._normalize_timestamp(
                getattr(
                    snapshot,
                    "timestamp",
                    None,
                )
            )

            if self._valid_price(snapshot_ltp):
                if snapshot_ts is not None:
                    age = (
                        now - snapshot_ts
                    ).total_seconds()

                    if (
                        0 <= age
                        <= self._max_ltp_age_seconds
                    ):
                        return (
                            float(snapshot_ltp),
                            "L5_STREAM",
                            snapshot_ts,
                        )

        # --------------------------------------------------------------
        # 2. Fresh engine tick cache
        # --------------------------------------------------------------
        with self._lock:
            cached = self._latest_ltp.get(
                symbol
            )

        if cached is not None:
            cached_price, cached_ts = cached

            age = (
                now - cached_ts
            ).total_seconds()

            if (
                self._valid_price(cached_price)
                and 0 <= age
                <= self._max_ltp_age_seconds
            ):
                return (
                    float(cached_price),
                    "ENGINE_LIVE_CACHE",
                    cached_ts,
                )

        # --------------------------------------------------------------
        # 3. No fresh market price
        # --------------------------------------------------------------
        return (
            None,
            "LIVE_LTP_UNAVAILABLE",
            None,
        )

    def record_tick_price(
        self,
        symbol: str,
        price: float,
        timestamp: Optional[datetime] = None,
    ) -> bool:
        """
        Optional direct tick hook.

        MarketStreamManager may call this on every valid tick in a future
        wiring update. It makes the engine independent of candle-close timing.
        """
        normalized_symbol = self._normalize_symbol(symbol)
        normalized_ts = self._normalize_timestamp(
            timestamp or now_ist()
        )

        if normalized_symbol is None:
            return False

        if normalized_ts is None:
            return False

        if not self._valid_price(price):
            return False

        with self._lock:
            self._latest_ltp[normalized_symbol] = (
                float(price),
                normalized_ts,
            )

        return True

    # ------------------------------------------------------------------
    # CANDLE PROCESSING
    # ------------------------------------------------------------------

    def _mark_processed_candle(
        self,
        symbol: str,
        candle_timestamp: datetime,
    ) -> bool:
        key = (
            symbol,
            candle_timestamp,
        )

        with self._lock:
            if key in self._processed_candle_keys:
                return False

            self._processed_candle_keys.add(key)
            self._processed_candle_order.append(key)

            # Proper bounded FIFO eviction using a deque + set.
            while len(self._processed_candle_order) > self._max_processed_keys:
                oldest = self._processed_candle_order.popleft()
                self._processed_candle_keys.discard(oldest)

        return True

    def _store_result(
        self,
        symbol: str,
        result: Dict[str, Any],
    ) -> None:
        with self._lock:
            self._predictions[symbol] = result

    def on_candle_close(
        self,
        candle_dict: Dict[str, Any],
        vwap: float,
        kite_client: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Evaluate one COMPLETED candle.

        The completed candle is used for strategy calculations.
        The latest real LTP is kept separate for signal-expiry validation.
        """
        symbol, candle_timestamp = (
            self._validate_candle_contract(
                candle_dict
            )
        )

        if symbol is None or candle_timestamp is None:
            logger.error(
                "[LiveSignalEngine] Rejected malformed completed candle: %r",
                candle_dict,
            )
            return None

        if not self._mark_processed_candle(
            symbol,
            candle_timestamp,
        ):
            logger.warning(
                "[LiveSignalEngine] Duplicate candle ignored: %s %s",
                symbol,
                candle_timestamp,
            )
            return self.get_prediction(symbol)

        data = self._copy_dataframe(
            live_market_state.get_candles_df(symbol)
        )

        # The candle-close event itself should be present in state. If a
        # caller invokes this engine directly without first updating state,
        # append the completed candle locally for this evaluation only.
        if data.empty:
            data = pd.DataFrame(
                [dict(candle_dict)]
            )
        else:
            if not (
                data["datetime"]
                == candle_timestamp
            ).any():
                data = pd.concat(
                    [
                        data,
                        pd.DataFrame(
                            [dict(candle_dict)]
                        ),
                    ],
                    ignore_index=True,
                )

        data = self._copy_dataframe(data)

        if data.empty:
            logger.error(
                "[LiveSignalEngine] No completed candle data available for %s",
                symbol,
            )
            return None

        # Ensure the callback's VWAP is available without overwriting an
        # authoritative per-candle VWAP supplied by the aggregator.
        if "vwap" not in data.columns:
            data["vwap"] = float(vwap) if self._valid_price(vwap) else None

        # Use the candle's own VWAP when available; only use the callback value
        # for the matching completed candle when the column is missing/invalid.
        if self._valid_price(vwap):
            mask = data["datetime"] == candle_timestamp
            if mask.any():
                existing = data.loc[mask, "vwap"]
                if existing.isna().all() or not self._valid_price(
                    existing.iloc[-1]
                ):
                    data.loc[mask, "vwap"] = float(vwap)

        try:
            candle_close = float(
                candle_dict["close"]
            )

            live_ltp, ltp_source, ltp_timestamp = (
                self._read_live_ltp(
                    symbol=symbol,
                )
            )

            if live_ltp is None:
                unavailable_result = {
                    "symbol": symbol,
                    "token": (
                        live_market_state.get_symbol_state(
                            symbol
                        ).token
                        if live_market_state.get_symbol_state(
                            symbol
                        )
                        else None
                    ),
                    "ltp": None,
                    "ltp_source": "LIVE_LTP_UNAVAILABLE",
                    "ltp_timestamp": None,
                    "candle_close": round(
                        candle_close,
                        2,
                    ),
                    "candle_timestamp": (
                        candle_timestamp.isoformat()
                    ),
                    "vwap": (
                        round(float(vwap), 4)
                        if self._valid_price(vwap)
                        else None
                    ),
                    "predictions": {},
                    "consensus": {
                        "direction": "NEUTRAL",
                        "agreeing_strategies": 0,
                        "total_strategies": 0,
                        "evaluable_strategies": 0,
                        "consensus_agreement_pct": None,
                        "label": "UNAVAILABLE",
                        "consensus_strategies": [],
                        "excluded_strategies": [],
                    },
                    "timestamp": now_ist_iso(),
                    "data_source": "KITE_STREAM",
                    "status": "UNAVAILABLE",
                    "error": (
                        "No fresh real-time LTP is available "
                        "for live strategy evaluation."
                    ),
                }

                self._store_result(
                    symbol,
                    unavailable_result,
                )

                return unavailable_result

            state = live_market_state.get_symbol_state(
                symbol
            )
            token = state.token if state else None
            book_snap = (
                state.book_snapshot
                if state is not None
                else None
            )

            # Enrich book snapshot with SSF futures/sector context
            ctx = ssf_context_store.get(symbol)
            if book_snap is not None:
                book_snap = BookSnapshot(
                    timestamp=book_snap.timestamp,
                    bids=book_snap.bids,
                    asks=book_snap.asks,
                    ltp=book_snap.ltp,
                    fut_ltp=ctx.fut_ltp,
                    fut_oi=ctx.fut_oi,
                    sector_ret_30m=ctx.sector_ret_30m,
                    stock_ret_30m=ctx.stock_ret_30m,
                    circuit_lower=ctx.circuit_lower or book_snap.circuit_lower,
                    circuit_upper=ctx.circuit_upper or book_snap.circuit_upper,
                )

            ssf_strat = None

            if (
                token is not None
                and isinstance(token, int)
                and token > 0
            ):
                ssf_strat = self._ssf_runtime.get_strategy(
                    symbol=symbol,
                    token=token,
                    current_price=float(live_ltp),
                )

            # current_ltp is explicitly the freshest verified market price,
            # not the completed candle's close unless no fresher price exists.
            preds, consensus = prediction_service.evaluate_symbol(
                symbol=symbol,
                df_15m=data,
                current_ltp=live_ltp,
                token=token,
                book_snapshot=book_snap,
                kite_client=kite_client,
                ssf_strategy=ssf_strat,
            )

            prediction_payload = {
                key: value.to_dict()
                if hasattr(value, "to_dict")
                else value
                for key, value in preds.items()
            }

            result = {
                "symbol": symbol,
                "token": token,
                "ltp": round(float(live_ltp), 2),
                "ltp_source": ltp_source,
                "ltp_timestamp": (
                    ltp_timestamp.isoformat()
                    if ltp_timestamp is not None
                    else None
                ),
                "candle_close": round(candle_close, 2),
                "candle_timestamp": candle_timestamp.isoformat(),
                "vwap": (
                    round(float(vwap), 4)
                    if self._valid_price(vwap)
                    else None
                ),
                "predictions": prediction_payload,
                "consensus": consensus,
                "timestamp": now_ist_iso(),
                "data_source": "KITE_STREAM",
            }

            self._store_result(
                symbol,
                result,
            )

            logger.info(
                "[LiveSignalEngine] Evaluated %s candle=%s ltp=%s source=%s "
                "consensus=%s (%s)",
                symbol,
                candle_timestamp,
                result["ltp"],
                ltp_source,
                consensus.get("direction"),
                consensus.get("label"),
            )

            return result

        except Exception as exc:
            # A failed symbol/strategy evaluation must remain visible as an
            # explicit error rather than disappearing and looking healthy.
            error_message = (
                f"Signal evaluation failed for {symbol}: "
                f"{type(exc).__name__}: {exc}"
            )

            logger.exception(
                "[LiveSignalEngine] %s",
                error_message,
            )

            error_result = {
                "symbol": symbol,
                "token": (
                    live_market_state.get_symbol_state(symbol).token
                    if live_market_state.get_symbol_state(symbol)
                    else None
                ),
                "ltp": None,
                "ltp_source": "ERROR",
                "ltp_timestamp": None,
                "candle_close": float(candle_dict["close"]),
                "candle_timestamp": candle_timestamp.isoformat(),
                "vwap": float(vwap) if self._valid_price(vwap) else None,
                "predictions": {},
                "consensus": {
                    "direction": "NEUTRAL",
                    "agreeing_strategies": 0,
                    "total_strategies": 0,
                    "evaluable_strategies": 0,
                    "consensus_agreement_pct": None,
                    "label": "ERROR",
                    "consensus_strategies": [],
                    "excluded_strategies": [],
                },
                "timestamp": now_ist_iso(),
                "data_source": "KITE_STREAM",
                "status": "ERROR",
                "error": error_message,
            }

            self._store_result(
                symbol,
                error_result,
            )

            return error_result

    # ------------------------------------------------------------------
    # SSF BOOK-UPDATE PATH
    # ------------------------------------------------------------------

    def on_book_update(
        self,
        symbol: str,
        snapshot: Any,
    ) -> None:
        """
        Route a live Level-5 book snapshot to the persistent SSF strategy.

        This is the PRIMARY entry point for SSF-L5-SRM as documented in the
        strategy header.  It is called from MarketStreamManager._on_book_update()
        on every tick that carries full depth, independently of candle closes.

        Other strategies (ORB/CPR/Dual-EMA/APEX/SIT) are NOT touched here.
        They run at candle-close time via on_candle_close().
        """
        clean = self._normalize_symbol(symbol)
        if clean is None:
            return

        if not isinstance(snapshot, BookSnapshot):
            # snapshot may come from the aggregator as the raw BookSnapshot;
            # if it is not the right type, skip rather than error.
            return

        state = live_market_state.get_symbol_state(clean)
        token = state.token if state else None
        ltp = snapshot.ltp

        if not self._valid_price(ltp):
            return

        if token is None or not isinstance(token, int) or token <= 0:
            logger.warning(
                "[LiveSignalEngine] No valid Kite token for SSF update: %s",
                clean,
            )
            return

        # Merge futures / sector context into the snapshot.
        ctx = ssf_context_store.get(clean)

        enriched = BookSnapshot(
            timestamp=snapshot.timestamp,
            bids=snapshot.bids,
            asks=snapshot.asks,
            ltp=snapshot.ltp,
            fut_ltp=ctx.fut_ltp,        # None until futures feed is wired
            fut_oi=ctx.fut_oi,          # None until futures feed is wired
            sector_ret_30m=ctx.sector_ret_30m,
            stock_ret_30m=ctx.stock_ret_30m,
            circuit_lower=ctx.circuit_lower or snapshot.circuit_lower,
            circuit_upper=ctx.circuit_upper or snapshot.circuit_upper,
        )

        try:
            strategy = self._ssf_runtime.get_strategy(
                symbol=clean,
                token=token,
                current_price=float(ltp),
            )
            strategy.on_book_update(enriched)
        except Exception as exc:
            logger.warning(
                "[LiveSignalEngine] SSF book-update failed for %s: %s: %s",
                clean,
                type(exc).__name__,
                exc,
            )

    def get_ssf_strategy(
        self,
        symbol: str,
        token: int = 0,
        current_price: float = 0.0,
    ) -> Any:
        return self._ssf_runtime.get_strategy(symbol, token, current_price)

    def prepare_ssf_session(
        self,
        symbol: str,
        reference_date: Any,
        kite_client: Optional[Any] = None,
    ) -> None:
        self._ssf_runtime.prepare_session(symbol, reference_date, kite_client)

    # ------------------------------------------------------------------
    # READ APIs
    # ------------------------------------------------------------------

    def get_prediction(
        self,
        symbol: str,
    ) -> Optional[Dict[str, Any]]:
        normalized_symbol = self._normalize_symbol(
            symbol
        )

        if normalized_symbol is None:
            return None

        with self._lock:
            prediction = self._predictions.get(
                normalized_symbol
            )

            return (
                dict(prediction)
                if prediction is not None
                else None
            )

    def get_all_predictions(
        self,
    ) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {
                symbol: dict(payload)
                for symbol, payload
                in self._predictions.items()
            }

    def get_live_signals(
        self,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                dict(payload)
                for payload
                in self._predictions.values()
            ]

    def reset(self) -> None:
        with self._lock:
            self._predictions.clear()
            self._latest_ltp.clear()
            self._processed_candle_keys.clear()
            self._processed_candle_order.clear()
        # Clear persistent SSF state so a new session starts clean.
        self._ssf_runtime.reset()
        ssf_context_store.reset()


live_signal_engine = LiveSignalEngine()
