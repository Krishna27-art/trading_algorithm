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

import numpy as np
import pandas as pd

from backend.data.time_utils import now_ist, now_ist_iso, now_ist_naive
from backend.database.db import SignalEventRecord, db_manager
from backend.strategy.base_strategy import SignalAction
from backend.strategy.prediction_service import (
    SingleStrategyPrediction,
    _positive_number,
    prediction_service,
)
from backend.strategy.ssf_l5_srm_strategy import BookSnapshot
from backend.streaming.crsd_live_runtime import crsd_live_runtime
from backend.streaming.live_market_state import live_market_state
from backend.streaming.ssf_runtime import ssf_live_runtime
from backend.streaming.ssf_runtime import ssf_context_store


logger = logging.getLogger("backend.streaming.live_signal_engine")


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
        self._crsd_runtime = crsd_live_runtime
        self._scanner_snapshot: Optional[Any] = None
        self._db = db_manager

    def set_scanner_snapshot(self, snapshot: Optional[Any]) -> None:
        with self._lock:
            self._scanner_snapshot = snapshot

    def get_scanner_snapshot(self) -> Optional[Any]:
        with self._lock:
            return self._scanner_snapshot

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
        Resolve only a verified real current market LTP.

        Resolution hierarchy:
            1. Fresh Level-5 snapshot LTP.
            2. Fresh engine-captured tick LTP.
            3. Fresh LiveMarketState tick LTP.

        A completed candle close is NEVER a current-LTP fallback.
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
        # 3. Fresh LiveMarketState tick
        # --------------------------------------------------------------
        if (
            state is not None
            and self._valid_price(state.ltp)
            and state.last_tick_time is not None
        ):
            st_ts = self._normalize_timestamp(state.last_tick_time)
            if st_ts is not None:
                age = (now - st_ts).total_seconds()
                if 0 <= age <= self._max_ltp_age_seconds:
                    return (
                        float(state.ltp),
                        "MARKET_STATE_LTP",
                        st_ts,
                    )

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
        """Record real exchange-timestamped tick LTP."""
        if timestamp is None:
            logger.warning(
                "[LiveSignalEngine] Rejecting tick without exchange timestamp."
            )
            return False

        normalized_symbol = self._normalize_symbol(symbol)
        normalized_ts = self._normalize_timestamp(timestamp)

        if normalized_symbol is None:
            return False

        if normalized_ts is None:
            return False

        if not self._valid_price(price):
            return False

        with self._lock:
            prior = self._latest_ltp.get(normalized_symbol)
            if (
                prior is not None
                and normalized_ts <= prior[1]
            ):
                logger.warning(
                    "[LiveSignalEngine] Ignoring out-of-order/duplicate "
                    "LTP for %s: %s <= %s",
                    normalized_symbol,
                    normalized_ts,
                    prior[1],
                )
                return False
            self._latest_ltp[normalized_symbol] = (
                float(price),
                normalized_ts,
            )

        # Evaluate active signals in database journal and in-memory prediction
        try:
            self._db.update_active_signal_tick(
                normalized_symbol,
                float(price),
                normalized_ts,
            )
            self.on_tick(
                normalized_symbol,
                float(price),
                normalized_ts,
            )
        except Exception as tick_exc:
            logger.debug(
                "[LiveSignalEngine] Tick evaluation error for %s: %s",
                normalized_symbol,
                tick_exc,
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

        key = (symbol, candle_timestamp)
        with self._lock:
            if key in self._processed_candle_keys:
                logger.warning(
                    "[LiveSignalEngine] Duplicate candle ignored: %s %s",
                    symbol,
                    candle_timestamp,
                )
                return self.get_prediction(symbol)

        self._crsd_runtime.on_candle(
            symbol,
            candle_dict,
        )

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

        # Do not create a synthetic VWAP column from one callback scalar across the whole DataFrame.
        if self._valid_price(vwap):
            mask = data["datetime"] == candle_timestamp
            if mask.any():
                existing = data.loc[mask, "vwap"] if "vwap" in data.columns else None

                if "vwap" not in data.columns:
                    data["vwap"] = pd.Series(
                        [np.nan] * len(data),
                        dtype="float64",
                    )

                if existing is None or existing.isna().all() or not self._valid_price(existing.iloc[-1]):
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
                    futures_updated_at=ctx.futures_updated_at,
                    sector_return_updated_at=ctx.sector_return_updated_at,
                    stock_return_updated_at=ctx.stock_return_updated_at,
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

            live_states = live_market_state.get_all_symbols_state()
            live_ltp_by_symbol = {
                sym: (
                    float(st.ltp),
                    st.last_tick_time,
                )
                for sym, st in live_states.items()
                if (
                    st is not None
                    and st.ltp is not None
                    and st.ltp > 0
                    and st.last_tick_time is not None
                )
            }

            crsd_ctx = self._crsd_runtime.get_context(
                symbol,
                kite_client=kite_client,
            )

            peer_ctx = None
            try:
                from backend.data.sector_peer_manager import SectorPeerManager
                peer_ctx = SectorPeerManager.build_peer_context(
                    symbol,
                    kite_client=kite_client,
                    allow_network_fetch=False,
                )
            except Exception as peer_exc:
                logger.debug(
                    "[LiveSignalEngine] Peer context construction skipped for %s: %s",
                    symbol,
                    peer_exc,
                )

            # current_ltp is explicitly the freshest verified market price,
            # never a completed candle's close.
            stock_metric = (
                self._scanner_snapshot.rankings.get(symbol)
                if self._scanner_snapshot is not None
                else None
            )

            preds, consensus = prediction_service.evaluate_symbol(
                symbol=symbol,
                df_15m=data,
                current_ltp=live_ltp,
                token=token,
                stock_metric=stock_metric,
                book_snapshot=book_snap,
                peer_context=peer_ctx,
                kite_client=kite_client,
                ssf_strategy=ssf_strat,
                live_ltp_by_symbol=live_ltp_by_symbol,
                crsd_context=crsd_ctx,
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

            if stock_metric is not None:
                result["scanner_rank"] = stock_metric.rank
                result["scanner_score"] = stock_metric.total_score
                result["scanner_bias"] = stock_metric.direction_bias

            self._store_result(
                symbol,
                result,
            )
            self._mark_processed_candle(
                symbol,
                candle_timestamp,
            )

            # Update active signal outcomes with completed candle
            try:
                self._db.update_active_signals_candle(symbol, candle_dict)
            except Exception as candle_eval_err:
                logger.debug(
                    "[LiveSignalEngine] Candle outcome evaluation error for %s: %s",
                    symbol,
                    candle_eval_err,
                )

            # Persist newly generated actionable signals to SQLite signal_events
            try:
                trading_date_str = candle_timestamp.strftime("%Y-%m-%d")
                for strat_key, p_obj in preds.items():
                    p_dir = getattr(p_obj, "direction", None) or (
                        p_obj.get("direction") if isinstance(p_obj, dict) else None
                    )
                    p_stat = getattr(p_obj, "status", None) or (
                        p_obj.get("status") if isinstance(p_obj, dict) else None
                    )
                    if p_dir in _VALID_DIRECTIONS and p_stat not in {
                        "WAITING",
                        "NO_TRADE",
                        "ERROR",
                        "UNAVAILABLE",
                    }:
                        entry_val = (
                            getattr(p_obj, "entry", None)
                            or (p_obj.get("entry") if isinstance(p_obj, dict) else None)
                            or live_ltp
                        )
                        stop_val = getattr(p_obj, "stop_loss", None) or (
                            p_obj.get("stop_loss") if isinstance(p_obj, dict) else None
                        )
                        target_val = getattr(p_obj, "target", None) or (
                            p_obj.get("target") if isinstance(p_obj, dict) else None
                        )
                        levels = (
                            getattr(p_obj, "levels", None)
                            or (p_obj.get("levels") if isinstance(p_obj, dict) else None)
                            or {}
                        )
                        if not stop_val and "stop" in levels:
                            stop_val = levels["stop"]
                        if not target_val and "target" in levels:
                            target_val = levels["target"]

                        if (
                            entry_val is not None
                            and stop_val is not None
                            and target_val is not None
                            and self._valid_price(entry_val)
                            and self._valid_price(stop_val)
                            and self._valid_price(target_val)
                        ):
                            sig_id = f"SIG_{strat_key.upper()}_{symbol}_{candle_timestamp.strftime('%Y%m%d_%H%M%S')}"
                            sig_rec = SignalEventRecord(
                                signal_id=sig_id,
                                trading_date=trading_date_str,
                                symbol=symbol,
                                instrument_token=token,
                                strategy=strat_key,
                                direction=p_dir,
                                generated_at=now_ist_naive(),
                                candle_timestamp=candle_timestamp,
                                entry_price=round(float(entry_val), 2),
                                stop_loss=round(float(stop_val), 2),
                                target=round(float(target_val), 2),
                                notes=(
                                    getattr(p_obj, "reason", "")
                                    or (
                                        p_obj.get("reason", "")
                                        if isinstance(p_obj, dict)
                                        else ""
                                    )
                                ),
                            )
                            self._db.record_signal(sig_rec)
            except Exception as persist_exc:
                logger.debug(
                    "[LiveSignalEngine] Signal journal recording error for %s: %s",
                    symbol,
                    persist_exc,
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
            return

        snapshot_ts = self._normalize_timestamp(
            getattr(snapshot, "timestamp", None)
        )
        if snapshot_ts is None:
            return

        from backend.data.time_utils import now_ist_naive
        now_naive = now_ist_naive()
        if hasattr(snapshot_ts, "tzinfo") and snapshot_ts.tzinfo is not None:
            snapshot_ts = snapshot_ts.replace(tzinfo=None)
        age = (now_naive - snapshot_ts).total_seconds()

        if age < 0 or age > self._max_ltp_age_seconds:
            logger.warning(
                "[LiveSignalEngine] Rejecting stale L5 snapshot for %s: age=%.1fs",
                clean,
                age,
            )
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
            fut_ltp=ctx.fut_ltp,
            fut_oi=ctx.fut_oi,
            sector_ret_30m=ctx.sector_ret_30m,
            stock_ret_30m=ctx.stock_ret_30m,
            circuit_lower=ctx.circuit_lower or snapshot.circuit_lower,
            circuit_upper=ctx.circuit_upper or snapshot.circuit_upper,
            futures_updated_at=ctx.futures_updated_at,
            sector_return_updated_at=ctx.sector_return_updated_at,
            stock_return_updated_at=ctx.stock_return_updated_at,
        )

        ltp_timestamp = snapshot_ts

        try:
            strategy = self._ssf_runtime.get_strategy(
                symbol=clean,
                token=token,
                current_price=float(ltp),
            )
            signal = strategy.on_book_update(enriched)

            if signal is None:
                ssf_prediction = SingleStrategyPrediction(
                    status="WAITING",
                    reason="Current Level-5 conditions do not qualify.",
                    levels=(
                        getattr(
                            strategy,
                            "last_features",
                            {}
                        )
                        or {}
                    ),
                    metrics={},
                    strategy="ssf_l5_srm",
                    symbol=clean,
                )
            else:
                ssf_prediction = (
                    prediction_service
                    ._prediction_from_signal(
                        status=(
                            "SSF_LONG"
                            if signal.action == SignalAction.BUY
                            else "SSF_SHORT"
                        ),
                        signal=signal,
                        ltp=float(ltp),
                        default_reason=(
                            signal.reason
                            or "SSF Level-5 generated a validated live signal."
                        ),
                        levels=(
                            getattr(
                                strategy,
                                "last_features",
                                {}
                            )
                            or {}
                        ),
                    )
                )

                # Persist SSF actionable signal to database journal
                if signal.action in (SignalAction.BUY, SignalAction.SELL):
                    try:
                        ssf_dir = "LONG" if signal.action == SignalAction.BUY else "SHORT"
                        ssf_entry = float(signal.entry_price or ltp)
                        ssf_stop = float(signal.stop_loss or 0.0)
                        ssf_target = float(signal.target or 0.0)
                        if (
                            self._valid_price(ssf_entry)
                            and self._valid_price(ssf_stop)
                            and self._valid_price(ssf_target)
                        ):
                            ssf_sig_id = f"SIG_SSF_{clean}_{snapshot_ts.strftime('%Y%m%d_%H%M%S')}"
                            ssf_rec = SignalEventRecord(
                                signal_id=ssf_sig_id,
                                trading_date=snapshot_ts.strftime("%Y-%m-%d"),
                                symbol=clean,
                                instrument_token=token,
                                strategy="ssf_l5_srm",
                                direction=ssf_dir,
                                generated_at=now_ist_naive(),
                                candle_timestamp=None,
                                entry_price=round(ssf_entry, 2),
                                stop_loss=round(ssf_stop, 2),
                                target=round(ssf_target, 2),
                                notes=signal.reason or "SSF Level-5 book entry",
                            )
                            self._db.record_signal(ssf_rec)
                    except Exception as ssf_persist_exc:
                        logger.debug(
                            "[LiveSignalEngine] SSF signal recording error for %s: %s",
                            clean,
                            ssf_persist_exc,
                        )

            with self._lock:
                prior_payload = self._predictions.get(clean)
                if isinstance(prior_payload, dict):
                    prior_per_strategy = dict(
                        prior_payload.get("predictions", {})
                    )
                else:
                    prior_per_strategy = {}

            # If candle-close strategies have not evaluated for this symbol yet,
            # run evaluation on the latest completed candle so all 8 strategies are active.
            if len(prior_per_strategy) <= 1:
                df_candles = live_market_state.get_candles_df(clean)
                if not df_candles.empty:
                    last_row = df_candles.iloc[-1].to_dict()
                    candle_ts = last_row.get("datetime")
                    if candle_ts is not None:
                        c_dict = {
                            "symbol": clean,
                            "datetime": candle_ts,
                            "open": float(last_row.get("open", 0.0)),
                            "high": float(last_row.get("high", 0.0)),
                            "low": float(last_row.get("low", 0.0)),
                            "close": float(last_row.get("close", 0.0)),
                            "volume": int(last_row.get("volume", 0)),
                        }
                        v_val = last_row.get("vwap")
                        try:
                            v_val = float(v_val) if v_val is not None else None
                        except (TypeError, ValueError):
                            v_val = None
                        self.on_candle_close(c_dict, vwap=v_val)
                        with self._lock:
                            prior_payload = self._predictions.get(clean)
                            if isinstance(prior_payload, dict):
                                prior_per_strategy = dict(
                                    prior_payload.get("predictions", {})
                                )

            with self._lock:
                prior_per_strategy.pop("SSF-L5-SRM", None)
                prior_per_strategy["ssf_l5_srm"] = ssf_prediction

                consensus = prediction_service.calculate_consensus(
                    prior_per_strategy
                )

            result = {
                "symbol": clean,
                "token": token,
                "ltp": round(float(ltp), 2),
                "ltp_source": "L5_STREAM",
                "ltp_timestamp": (
                    ltp_timestamp.isoformat()
                    if ltp_timestamp is not None
                    else None
                ),
                "candle_close": (
                    prior_payload.get("candle_close")
                    if isinstance(prior_payload, dict)
                    else None
                ),
                "candle_timestamp": (
                    prior_payload.get("candle_timestamp")
                    if isinstance(prior_payload, dict)
                    else None
                ),
                "vwap": (
                    prior_payload.get("vwap")
                    if isinstance(prior_payload, dict)
                    else None
                ),
                "predictions": {
                    key: (
                        value.to_dict()
                        if hasattr(value, "to_dict")
                        else value
                    )
                    for key, value in prior_per_strategy.items()
                },
                "consensus": consensus,
                "timestamp": now_ist_iso(),
                "data_source": "KITE_STREAM",
            }

            self._store_result(clean, result)

        except Exception as exc:
            logger.warning(
                "[LiveSignalEngine] SSF book-update failed for %s: %s: %s",
                clean,
                type(exc).__name__,
                exc,
            )
            ssf_prediction = SingleStrategyPrediction(
                status="ERROR",
                reason=f"SSF evaluation failed: {type(exc).__name__}: {exc}",
                levels={},
                metrics={},
                strategy="ssf_l5_srm",
                symbol=clean,
            )
            with self._lock:
                prior_payload = self._predictions.get(clean)
                if isinstance(prior_payload, dict):
                    prior_per_strategy = dict(
                        prior_payload.get("predictions", {})
                    )
                    prior_per_strategy["ssf_l5_srm"] = ssf_prediction
                    consensus = prediction_service.calculate_consensus(prior_per_strategy)
                    prior_payload["predictions"] = {
                        k: (v.to_dict() if hasattr(v, "to_dict") else v)
                        for k, v in prior_per_strategy.items()
                    }
                    prior_payload["consensus"] = consensus
                    self._store_result(clean, prior_payload)

    def on_tick(
        self,
        symbol: str,
        price: float,
        timestamp: datetime,
    ) -> None:
        """
        Monitor active intrabar stop/target/time-exits on real ticks.
        """
        clean = self._normalize_symbol(symbol)
        if clean is None or not self._valid_price(price):
            return

        with self._lock:
            payload = self._predictions.get(clean)
            if not isinstance(payload, dict):
                return
            predictions = dict(payload.get("predictions", {}))

        updated = False
        for strat_name, pred in list(predictions.items()):
            if not isinstance(pred, dict):
                continue
            status = pred.get("status")
            direction = pred.get("direction")
            if status in {"UNAVAILABLE", "ERROR", "NO_TRADE", "WAITING", "STOPPED_OUT", "TARGET_HIT"}:
                continue
            if direction not in {"LONG", "SHORT"}:
                continue

            levels = pred.get("levels") or {}
            stop = _positive_number(levels.get("stop") or pred.get("stop_loss"))
            target = _positive_number(levels.get("target") or pred.get("target"))

            breached = False
            exit_reason = None
            if direction == "LONG":
                if stop is not None and price <= stop:
                    breached = True
                    exit_reason = f"Intrabar stop breached: price {price:.2f} <= stop {stop:.2f}"
                elif target is not None and price >= target:
                    breached = True
                    exit_reason = f"Intrabar target reached: price {price:.2f} >= target {target:.2f}"
            elif direction == "SHORT":
                if stop is not None and price >= stop:
                    breached = True
                    exit_reason = f"Intrabar stop breached: price {price:.2f} >= stop {stop:.2f}"
                elif target is not None and price <= target:
                    breached = True
                    exit_reason = f"Intrabar target reached: price {price:.2f} <= target {target:.2f}"

            if breached:
                updated_pred = dict(pred)
                updated_pred["status"] = "NO_TRADE"
                updated_pred["direction"] = "NEUTRAL"
                updated_pred["reason"] = exit_reason
                predictions[strat_name] = updated_pred
                updated = True

        # Refresh published LTP and timestamp on every valid incoming tick
        result = dict(payload)
        result["ltp"] = round(float(price), 2)
        result["ltp_timestamp"] = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)
        if updated:
            consensus = prediction_service.calculate_consensus(predictions)
            result["predictions"] = predictions
            result["consensus"] = consensus
        self._store_result(clean, result)

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
        # Clear persistent SSF and CRSD state so a new session starts clean.
        self._ssf_runtime.reset()
        self._crsd_runtime.reset()
        ssf_context_store.reset()


live_signal_engine = LiveSignalEngine()
