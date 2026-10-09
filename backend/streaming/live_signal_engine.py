from __future__ import annotations

import logging
import math
import re
import sqlite3
import threading
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

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
_NON_ACTIONABLE_STATUSES = {"WAITING", "NO_TRADE", "ERROR", "UNAVAILABLE"}
_DEFAULT_TIMEFRAME = "15m"
_TIMEFRAME_PATTERN = re.compile(r"^(\d+)\s*(m|min|h|d)$")
_CLOCK_SKEW_TOLERANCE_SECONDS = 5.0

_CLOSED = "CLOSED"
_OPEN = "OPEN"
_DB_ERROR = "DB_ERROR"

_PERSISTED = "PERSISTED"
_ALREADY_PERSISTED = "ALREADY_PERSISTED"
_PERSIST_FAILED = "PERSIST_FAILED"

_CLAIMED = "CLAIMED"
_DUPLICATE = "DUPLICATE"
_IN_FLIGHT = "IN_FLIGHT"
_OUT_OF_ORDER = "OUT_OF_ORDER"

_SSF_STRATEGY = "ssf_l5_srm"


class LiveSignalEngine:
    def __init__(
        self,
        max_candles_per_series: int = 256,
        max_ltp_age_seconds: int = 120,
        max_signal_age_seconds: int = 1200,
        require_persistence: bool = True,
    ) -> None:
        if max_candles_per_series <= 0:
            raise ValueError("max_candles_per_series must be > 0")
        if max_ltp_age_seconds <= 0:
            raise ValueError("max_ltp_age_seconds must be > 0")
        if max_signal_age_seconds <= 0:
            raise ValueError("max_signal_age_seconds must be > 0")

        self._lock = threading.RLock()

        self._predictions: Dict[str, Dict[str, Any]] = {}
        self._latest_ltp: Dict[str, Tuple[float, datetime]] = {}

        self._candle_ids: Dict[Tuple[str, str], "OrderedDict[datetime, None]"] = {}
        self._last_candle_time: Dict[Tuple[str, str], datetime] = {}
        self._inflight_candles: Set[Tuple[str, str, datetime]] = set()

        self._max_candles_per_series = int(max_candles_per_series)
        self._max_ltp_age_seconds = int(max_ltp_age_seconds)
        self._max_signal_age_seconds = int(max_signal_age_seconds)
        self._require_persistence = bool(require_persistence)

        self._ssf_runtime = ssf_live_runtime
        self._crsd_runtime = crsd_live_runtime
        self._scanner_snapshot: Optional[Any] = None
        self._db = db_manager
        self._closed_signals_today: Dict[Tuple[str, str, str], str] = {}

    def mark_signal_closed(
        self,
        symbol: str,
        strategy: str,
        trading_date: str,
        reason: str = "",
    ) -> None:
        clean = (symbol or "").strip().upper()
        strat = (strategy or "").strip().lower()
        if not clean or not strat or not trading_date:
            return
        key = (clean, strat, str(trading_date))
        with self._lock:
            self._closed_signals_today[key] = str(reason)

    def signal_closed_state(
        self,
        symbol: str,
        strategy: str,
        trading_date: str,
    ) -> str:
        clean = (symbol or "").strip().upper()
        strat = (strategy or "").strip().lower()
        if not clean or not strat or not trading_date:
            return _DB_ERROR
        key = (clean, strat, str(trading_date))
        with self._lock:
            if key in self._closed_signals_today:
                return _CLOSED
        try:
            with self._db._get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT outcome FROM signal_events WHERE symbol = ? AND strategy = ? AND trading_date = ? AND signal_status = 'CLOSED' LIMIT 1",
                    (clean, strat, str(trading_date)),
                )
                row = cur.fetchone()
        except Exception:
            logger.exception(
                "[LiveSignalEngine] Closed-state lookup failed for %s/%s/%s; state is UNKNOWN and treated as closed",
                clean,
                strat,
                trading_date,
            )
            return _DB_ERROR
        if row:
            with self._lock:
                self._closed_signals_today[key] = f"Closed with outcome {row[0]}"
            return _CLOSED
        return _OPEN

    def is_signal_closed(
        self,
        symbol: str,
        strategy: str,
        trading_date: str,
    ) -> bool:
        return self.signal_closed_state(symbol, strategy, trading_date) != _OPEN

    def get_closed_reason(
        self,
        symbol: str,
        strategy: str,
        trading_date: str,
    ) -> Optional[str]:
        clean = (symbol or "").strip().upper()
        strat = (strategy or "").strip().lower()
        key = (clean, strat, str(trading_date))
        with self._lock:
            return self._closed_signals_today.get(key)

    def set_scanner_snapshot(self, snapshot: Optional[Any]) -> None:
        with self._lock:
            self._scanner_snapshot = snapshot

    def get_scanner_snapshot(self) -> Optional[Any]:
        with self._lock:
            return self._scanner_snapshot

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
        data = data.drop_duplicates(subset=["datetime"], keep="last")
        return data.reset_index(drop=True)

    @staticmethod
    def _pred_field(p_obj: Any, name: str) -> Any:
        value = getattr(p_obj, name, None)
        if value is None and isinstance(p_obj, dict):
            value = p_obj.get(name)
        return value

    @staticmethod
    def _prediction_to_dict(value: Any) -> Any:
        converted = value.to_dict() if hasattr(value, "to_dict") else value
        return dict(converted) if isinstance(converted, dict) else converted

    @staticmethod
    def _empty_consensus(label: str) -> Dict[str, Any]:
        return {
            "direction": "NEUTRAL",
            "agreeing_strategies": 0,
            "total_strategies": 0,
            "evaluable_strategies": 0,
            "consensus_agreement_pct": None,
            "label": label,
            "consensus_strategies": [],
            "excluded_strategies": [],
        }

    @staticmethod
    def _token_for(symbol: str) -> Optional[int]:
        state = live_market_state.get_symbol_state(symbol)
        return state.token if state else None

    @staticmethod
    def _resolve_timeframe(
        candle_dict: Dict[str, Any],
    ) -> Optional[Tuple[str, timedelta]]:
        raw = candle_dict.get("timeframe") or _DEFAULT_TIMEFRAME
        timeframe = str(raw).strip().lower()
        match = _TIMEFRAME_PATTERN.match(timeframe)
        if match is None:
            return None
        amount = int(match.group(1))
        if amount <= 0:
            return None
        unit = match.group(2)
        if unit in ("m", "min"):
            return timeframe, timedelta(minutes=amount)
        if unit == "h":
            return timeframe, timedelta(hours=amount)
        return timeframe, timedelta(days=amount)

    def _validate_candle_contract(
        self,
        candle_dict: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[datetime]]:
        if not isinstance(candle_dict, dict):
            return None, None

        symbol = self._normalize_symbol(candle_dict.get("symbol"))
        if symbol is None:
            return None, None

        timestamp = self._normalize_timestamp(candle_dict.get("datetime"))
        if timestamp is None:
            return None, None

        for field in ("open", "high", "low", "close"):
            if not self._valid_price(candle_dict.get(field)):
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

        if not (low <= open_price <= high and low <= close <= high):
            return None, None

        return symbol, timestamp

    def _compute_freshness(
        self,
        reference: Optional[datetime],
        max_age_seconds: int,
        as_of: datetime,
    ) -> Dict[str, Any]:
        block: Dict[str, Any] = {
            "status": "UNKNOWN",
            "is_current": False,
            "age_seconds": None,
            "max_age_seconds": int(max_age_seconds),
            "reference_time": (
                reference.isoformat() if reference is not None else None
            ),
            "as_of": as_of.isoformat(),
        }
        if reference is None:
            return block

        age = (as_of - reference).total_seconds()
        if age < 0:
            if age >= -_CLOCK_SKEW_TOLERANCE_SECONDS:
                age = 0.0
            else:
                block["age_seconds"] = round(age, 1)
                block["status"] = "NOT_YET_COMPLETE"
                return block

        block["age_seconds"] = round(age, 1)
        if age <= max_age_seconds:
            block["status"] = "FRESH"
            block["is_current"] = True
        else:
            block["status"] = "STALE"
        return block

    def _refresh_freshness_block(
        self,
        block: Any,
        as_of: datetime,
    ) -> Dict[str, Any]:
        if not isinstance(block, dict):
            return self._compute_freshness(None, self._max_signal_age_seconds, as_of)
        reference = self._normalize_timestamp(block.get("reference_time"))
        try:
            max_age = int(block.get("max_age_seconds"))
        except (TypeError, ValueError):
            max_age = self._max_signal_age_seconds
        if max_age <= 0:
            max_age = self._max_signal_age_seconds
        return self._compute_freshness(reference, max_age, as_of)

    def _with_current_freshness(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        refreshed = dict(payload)
        as_of = now_ist_naive()
        refreshed["data_freshness"] = self._refresh_freshness_block(
            refreshed.get("data_freshness"),
            as_of,
        )
        predictions = refreshed.get("predictions")
        if isinstance(predictions, dict):
            updated: Dict[str, Any] = {}
            for key, value in predictions.items():
                if isinstance(value, dict):
                    value = dict(value)
                    if "data_freshness" in value:
                        fb = self._refresh_freshness_block(value.get("data_freshness"), as_of)
                        value["data_freshness"] = fb
                        value["freshness"] = fb
                        if fb.get("status") != "FRESH":
                            value["status"] = "UNAVAILABLE"
                            value["direction"] = None
                            value["entry"] = None
                            value["stop_loss"] = None
                            value["target"] = None
                            value["reason"] = f"Stale strategy result withheld: {fb.get('status')}"
                updated[key] = value
            refreshed["predictions"] = updated
            refreshed["consensus"] = prediction_service.calculate_consensus(updated)
        return refreshed

    @staticmethod
    def _stamp(
        symbol: str,
        signal_time: Optional[str],
        source_candle_time: Optional[str],
        evaluation_time: str,
        freshness: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "signal_time": signal_time,
            "source_candle_time": source_candle_time,
            "evaluation_time": evaluation_time,
            "data_freshness": freshness,
        }

    def _read_live_ltp(
        self,
        symbol: str,
    ) -> Tuple[Optional[float], str, Optional[datetime]]:
        state = live_market_state.get_symbol_state(symbol)
        now = now_ist().replace(tzinfo=None)

        if state is not None and state.book_snapshot is not None:
            snapshot = state.book_snapshot
            snapshot_ltp = getattr(snapshot, "ltp", None)
            snapshot_ts = self._normalize_timestamp(
                getattr(snapshot, "timestamp", None)
            )
            if self._valid_price(snapshot_ltp) and snapshot_ts is not None:
                age = (now - snapshot_ts).total_seconds()
                if 0 <= age <= self._max_ltp_age_seconds:
                    return float(snapshot_ltp), "L5_STREAM", snapshot_ts

        with self._lock:
            cached = self._latest_ltp.get(symbol)

        if cached is not None:
            cached_price, cached_ts = cached
            age = (now - cached_ts).total_seconds()
            if (
                self._valid_price(cached_price)
                and 0 <= age <= self._max_ltp_age_seconds
            ):
                return float(cached_price), "ENGINE_LIVE_CACHE", cached_ts

        if (
            state is not None
            and self._valid_price(state.ltp)
            and state.last_tick_time is not None
        ):
            st_ts = self._normalize_timestamp(state.last_tick_time)
            if st_ts is not None:
                age = (now - st_ts).total_seconds()
                if 0 <= age <= self._max_ltp_age_seconds:
                    return float(state.ltp), "MARKET_STATE_LTP", st_ts

        return None, "LIVE_LTP_UNAVAILABLE", None

    def record_tick_price(
        self,
        symbol: str,
        price: float,
        timestamp: Optional[datetime] = None,
    ) -> bool:
        if timestamp is None:
            logger.warning(
                "[LiveSignalEngine] Rejecting tick without exchange timestamp."
            )
            return False

        normalized_symbol = self._normalize_symbol(symbol)
        normalized_ts = self._normalize_timestamp(timestamp)

        if normalized_symbol is None or normalized_ts is None:
            return False

        if not self._valid_price(price):
            return False

        with self._lock:
            prior = self._latest_ltp.get(normalized_symbol)
            if prior is not None and normalized_ts <= prior[1]:
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

        resolved_sigs = None
        try:
            resolved_sigs = self._db.update_active_signal_tick(
                normalized_symbol,
                float(price),
                normalized_ts,
            )
        except Exception:
            logger.exception(
                "[LiveSignalEngine] Journal tick outcome update failed for %s",
                normalized_symbol,
            )

        for res_sig in resolved_sigs or []:
            s_sym = res_sig.get("symbol")
            s_strat = res_sig.get("strategy")
            s_date = res_sig.get("trading_date")
            if s_sym and s_strat and s_date:
                self.mark_signal_closed(
                    s_sym,
                    s_strat,
                    s_date,
                    f"Resolved with outcome {res_sig.get('outcome', 'CLOSED')} at {res_sig.get('outcome_price')}",
                )

        try:
            self.on_tick(normalized_symbol, float(price), normalized_ts)
        except Exception:
            logger.exception(
                "[LiveSignalEngine] Tick evaluation failed for %s",
                normalized_symbol,
            )

        return True

    def _claim_candle(
        self,
        symbol: str,
        timeframe: str,
        candle_timestamp: datetime,
    ) -> str:
        key = (symbol, timeframe)
        with self._lock:
            seen = self._candle_ids.get(key)
            if seen is not None and candle_timestamp in seen:
                return _DUPLICATE
            if (symbol, timeframe, candle_timestamp) in self._inflight_candles:
                return _IN_FLIGHT
            last = self._last_candle_time.get(key)
            if last is not None and candle_timestamp < last:
                return _OUT_OF_ORDER
            self._inflight_candles.add((symbol, timeframe, candle_timestamp))
            return _CLAIMED

    def _finalize_candle(
        self,
        symbol: str,
        timeframe: str,
        candle_timestamp: datetime,
        committed: bool,
    ) -> None:
        key = (symbol, timeframe)
        with self._lock:
            self._inflight_candles.discard((symbol, timeframe, candle_timestamp))
            if not committed:
                return
            seen = self._candle_ids.get(key)
            if seen is None:
                seen = OrderedDict()
                self._candle_ids[key] = seen
            seen[candle_timestamp] = None
            while len(seen) > self._max_candles_per_series:
                seen.popitem(last=False)
            last = self._last_candle_time.get(key)
            if last is None or candle_timestamp > last:
                self._last_candle_time[key] = candle_timestamp

    def _store_result(
        self,
        symbol: str,
        result: Dict[str, Any],
    ) -> None:
        with self._lock:
            self._predictions[symbol] = result

    def _publish_candle_result(
        self,
        symbol: str,
        result: Dict[str, Any],
        candle_timestamp: datetime,
    ) -> bool:
        with self._lock:
            existing = self._predictions.get(symbol)
            if isinstance(existing, dict):
                existing_ts = self._normalize_timestamp(
                    existing.get("candle_timestamp")
                )
                if existing_ts is not None and existing_ts > candle_timestamp:
                    return False
            self._predictions[symbol] = result
            return True

    def _signal_exists(self, signal_id: str) -> Optional[bool]:
        try:
            with self._db._get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT 1 FROM signal_events WHERE signal_id = ? LIMIT 1",
                    (signal_id,),
                )
                return cur.fetchone() is not None
        except Exception:
            logger.exception(
                "[LiveSignalEngine] Journal existence check failed for %s",
                signal_id,
            )
            return None

    def _persist_signal(self, record: SignalEventRecord) -> str:
        exists = self._signal_exists(record.signal_id)
        if exists is True:
            return _ALREADY_PERSISTED
        if exists is None:
            return _PERSIST_FAILED

        try:
            outcome = self._db.record_signal(record)
        except sqlite3.IntegrityError:
            return _ALREADY_PERSISTED
        except Exception:
            logger.exception(
                "[LiveSignalEngine] Journal write failed for signal %s",
                record.signal_id,
            )
            return _PERSIST_FAILED

        if outcome is False:
            if self._signal_exists(record.signal_id) is True:
                return _ALREADY_PERSISTED
            logger.error(
                "[LiveSignalEngine] Journal rejected signal %s",
                record.signal_id,
            )
            return _PERSIST_FAILED

        return _PERSISTED

    def _is_actionable(self, p_obj: Any) -> bool:
        return (
            self._pred_field(p_obj, "direction") in _VALID_DIRECTIONS
            and self._pred_field(p_obj, "status") not in _NON_ACTIONABLE_STATUSES
        )

    def _build_signal_record(
        self,
        symbol: str,
        strat_key: str,
        p_obj: Any,
        token: Optional[int],
        candle_timestamp: datetime,
        fallback_entry: float,
    ) -> Optional[SignalEventRecord]:
        levels = self._pred_field(p_obj, "levels")
        if not isinstance(levels, dict):
            levels = {}

        entry = self._pred_field(p_obj, "entry") or fallback_entry
        stop = self._pred_field(p_obj, "stop_loss") or levels.get("stop")
        target = self._pred_field(p_obj, "target") or levels.get("target")

        if not (
            self._valid_price(entry)
            and self._valid_price(stop)
            and self._valid_price(target)
        ):
            return None

        return SignalEventRecord(
            signal_id=f"SIG_{strat_key.upper()}_{symbol}_{candle_timestamp.strftime('%Y%m%d_%H%M%S')}",
            trading_date=candle_timestamp.strftime("%Y-%m-%d"),
            symbol=symbol,
            instrument_token=token,
            strategy=strat_key,
            direction=self._pred_field(p_obj, "direction"),
            generated_at=now_ist_naive(),
            candle_timestamp=candle_timestamp,
            entry_price=round(float(entry), 2),
            stop_loss=round(float(stop), 2),
            target=round(float(target), 2),
            notes=self._pred_field(p_obj, "reason") or "",
        )

    def _suppressed_prediction(
        self,
        symbol: str,
        strat_key: str,
        p_obj: Any,
        reason: str,
    ) -> SingleStrategyPrediction:
        return SingleStrategyPrediction(
            status="NO_TRADE",
            direction="NEUTRAL",
            reason=reason,
            levels=self._pred_field(p_obj, "levels") or {},
            metrics=self._pred_field(p_obj, "metrics") or {},
            strategy=strat_key,
            symbol=symbol,
        )

    def _signal_block_reason(
        self,
        symbol: str,
        strat_key: str,
        trading_date: str,
        freshness: Dict[str, Any],
    ) -> Optional[str]:
        state = self.signal_closed_state(symbol, strat_key, trading_date)
        if state == _CLOSED:
            closed_reason = (
                self.get_closed_reason(symbol, strat_key, trading_date)
                or "Session signal already resolved"
            )
            return f"{strat_key.upper()} signal concluded for this session: {closed_reason}"
        if state == _DB_ERROR:
            return (
                f"{strat_key.upper()} signal state is unknown because the journal "
                "database is unavailable; signal withheld"
            )
        if not freshness.get("is_current", False):
            return (
                f"{strat_key.upper()} source candle is not current "
                f"(status {freshness.get('status')}, age {freshness.get('age_seconds')}s); "
                "signal withheld"
            )
        return None

    def on_candle_close(
        self,
        candle_dict: Dict[str, Any],
        vwap: float,
        kite_client: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        symbol, candle_timestamp = self._validate_candle_contract(candle_dict)

        if symbol is None or candle_timestamp is None:
            logger.error(
                "[LiveSignalEngine] Rejected malformed completed candle: %r",
                candle_dict,
            )
            return None

        resolved = self._resolve_timeframe(candle_dict)
        if resolved is None:
            logger.error(
                "[LiveSignalEngine] Rejected completed candle with invalid timeframe: %r",
                candle_dict,
            )
            return None
        timeframe, delta = resolved

        claim = self._claim_candle(symbol, timeframe, candle_timestamp)
        if claim != _CLAIMED:
            logger.warning(
                "[LiveSignalEngine] Candle not processed (%s): %s %s %s",
                claim,
                symbol,
                timeframe,
                candle_timestamp,
            )
            return self.get_prediction(symbol)

        committed = False
        try:
            result, committed = self._evaluate_completed_candle(
                symbol,
                candle_timestamp,
                delta,
                candle_dict,
                vwap,
                kite_client,
            )
            return result
        finally:
            self._finalize_candle(symbol, timeframe, candle_timestamp, committed)

    def _evaluate_completed_candle(
        self,
        symbol: str,
        candle_timestamp: datetime,
        delta: timedelta,
        candle_dict: Dict[str, Any],
        vwap: float,
        kite_client: Optional[Any],
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        self._crsd_runtime.on_candle(symbol, candle_dict)

        data = self._copy_dataframe(live_market_state.get_candles_df(symbol))

        if data.empty:
            data = pd.DataFrame([dict(candle_dict)])
        elif not (data["datetime"] == candle_timestamp).any():
            data = pd.concat(
                [data, pd.DataFrame([dict(candle_dict)])],
                ignore_index=True,
            )

        data = self._copy_dataframe(data)

        if data.empty:
            logger.error(
                "[LiveSignalEngine] No completed candle data available for %s",
                symbol,
            )
            return None, False

        if "vwap" not in data.columns or data["vwap"].isna().any():
            from backend.indicators.vwap import calculate_session_vwap

            data["vwap"] = calculate_session_vwap(data).to_numpy()

        if self._valid_price(vwap):
            mask = data["datetime"] == candle_timestamp
            if mask.any():
                data.loc[mask, "vwap"] = float(vwap)

        source_candle_iso = candle_timestamp.isoformat()
        candle_end = candle_timestamp + delta
        vwap_value = round(float(vwap), 4) if self._valid_price(vwap) else None

        try:
            candle_close = float(candle_dict["close"])
            live_ltp, ltp_source, ltp_timestamp = self._read_live_ltp(symbol=symbol)

            evaluation_dt = now_ist_naive()
            evaluation_iso = now_ist_iso()
            freshness = self._compute_freshness(
                candle_end,
                self._max_signal_age_seconds,
                evaluation_dt,
            )

            state = live_market_state.get_symbol_state(symbol)
            token = state.token if state else None

            if live_ltp is None:
                unavailable_result = {
                    "symbol": symbol,
                    "token": token,
                    "ltp": None,
                    "ltp_source": "LIVE_LTP_UNAVAILABLE",
                    "ltp_timestamp": None,
                    "candle_close": round(candle_close, 2),
                    "candle_timestamp": source_candle_iso,
                    "vwap": vwap_value,
                    "predictions": {},
                    "consensus": self._empty_consensus("UNAVAILABLE"),
                    "timestamp": evaluation_iso,
                    "data_source": "KITE_STREAM",
                    "status": "UNAVAILABLE",
                    "error": (
                        "No fresh real-time LTP is available "
                        "for live strategy evaluation."
                    ),
                }
                unavailable_result.update(
                    self._stamp(
                        symbol,
                        None,
                        source_candle_iso,
                        evaluation_iso,
                        freshness,
                    )
                )
                self._publish_candle_result(
                    symbol,
                    unavailable_result,
                    candle_timestamp,
                )
                return unavailable_result, False

            book_snap = state.book_snapshot if state is not None else None

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
            if token is not None and isinstance(token, int) and token > 0:
                ssf_strat = self._ssf_runtime.get_strategy(
                    symbol=symbol,
                    token=token,
                    current_price=float(live_ltp),
                )

            live_states = live_market_state.get_all_symbols_state()
            live_ltp_by_symbol = {
                sym: (float(st.ltp), st.last_tick_time)
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
            preds = dict(preds)

            try:
                self._db.update_active_signals_candle(symbol, candle_dict)
            except Exception:
                logger.exception(
                    "[LiveSignalEngine] Candle outcome evaluation failed for %s; "
                    "journal closed-state may be behind",
                    symbol,
                )

            session_date_str = candle_timestamp.strftime("%Y-%m-%d")
            lifecycle: Dict[str, Dict[str, Any]] = {}
            suppressed = False

            for strat_key, p_obj in list(preds.items()):
                if self._pred_field(p_obj, "direction") not in _VALID_DIRECTIONS:
                    continue

                block_reason = self._signal_block_reason(
                    symbol,
                    strat_key,
                    session_date_str,
                    freshness,
                )
                record = None
                persisted = False

                if block_reason is None and self._is_actionable(p_obj):
                    record = self._build_signal_record(
                        symbol,
                        strat_key,
                        p_obj,
                        token,
                        candle_timestamp,
                        float(live_ltp),
                    )
                    if record is None:
                        logger.error(
                            "[LiveSignalEngine] Actionable %s signal for %s has no valid entry/stop/target and cannot be journaled",
                            strat_key,
                            symbol,
                        )
                        if self._require_persistence:
                            block_reason = (
                                f"{strat_key.upper()} signal lacks valid entry, stop and "
                                "target and cannot be journaled; signal withheld"
                            )
                    else:
                        persist_state = self._persist_signal(record)
                        persisted = persist_state != _PERSIST_FAILED
                        if not persisted:
                            logger.error(
                                "[LiveSignalEngine] Signal %s generated but NOT persisted",
                                record.signal_id,
                            )
                            if self._require_persistence:
                                block_reason = (
                                    f"{strat_key.upper()} signal could not be written to the "
                                    "journal; signal withheld"
                                )

                if block_reason is not None:
                    logger.warning(
                        "[LiveSignalEngine] Suppressed %s signal for %s: %s",
                        strat_key,
                        symbol,
                        block_reason,
                    )
                    preds[strat_key] = self._suppressed_prediction(
                        symbol,
                        strat_key,
                        p_obj,
                        block_reason,
                    )
                    suppressed = True
                    if record is not None:
                        lifecycle[strat_key] = {
                            "signal_id": record.signal_id,
                            "generated": True,
                            "persisted": persisted,
                            "published": False,
                        }
                elif record is not None:
                    lifecycle[strat_key] = {
                        "signal_id": record.signal_id,
                        "generated": True,
                        "persisted": persisted,
                        "published": True,
                    }

            if suppressed:
                consensus = prediction_service.calculate_consensus(preds)

            prediction_payload: Dict[str, Any] = {}
            for key, value in preds.items():
                entry = self._prediction_to_dict(value)
                if isinstance(entry, dict):
                    entry.update(
                        self._stamp(
                            symbol,
                            evaluation_iso,
                            source_candle_iso,
                            evaluation_iso,
                            freshness,
                        )
                    )
                    if key in lifecycle:
                        entry["signal_id"] = lifecycle[key]["signal_id"]
                        entry["lifecycle"] = dict(lifecycle[key])
                prediction_payload[key] = entry

            result = {
                "symbol": symbol,
                "token": token,
                "ltp": round(float(live_ltp), 2),
                "ltp_source": ltp_source,
                "ltp_timestamp": (
                    ltp_timestamp.isoformat() if ltp_timestamp is not None else None
                ),
                "candle_close": round(candle_close, 2),
                "candle_timestamp": source_candle_iso,
                "vwap": vwap_value,
                "predictions": prediction_payload,
                "consensus": consensus,
                "timestamp": evaluation_iso,
                "data_source": "KITE_STREAM",
            }
            result.update(
                self._stamp(
                    symbol,
                    evaluation_iso,
                    source_candle_iso,
                    evaluation_iso,
                    freshness,
                )
            )

            try:
                from backend.streaming.market_stream_manager import (
                    market_stream_manager,
                )

                vp_facts = market_stream_manager.get_volume_profile(symbol, live_ltp)
                result["volume_profile"] = vp_facts.to_dict()
            except Exception as vp_exc:
                logger.debug(
                    "[LiveSignalEngine] Volume profile unavailable for %s: %s",
                    symbol,
                    vp_exc,
                )

            if stock_metric is not None:
                result["scanner_rank"] = stock_metric.rank
                result["scanner_score"] = stock_metric.total_score
                result["scanner_bias"] = stock_metric.direction_bias

            published = self._publish_candle_result(
                symbol,
                result,
                candle_timestamp,
            )
            if not published:
                logger.warning(
                    "[LiveSignalEngine] Result for %s candle %s not published: newer candle already published",
                    symbol,
                    candle_timestamp,
                )
                return self.get_prediction(symbol), True

            logger.info(
                "[LiveSignalEngine] Evaluated %s candle=%s ltp=%s source=%s "
                "freshness=%s consensus=%s (%s)",
                symbol,
                candle_timestamp,
                result["ltp"],
                ltp_source,
                freshness["status"],
                consensus.get("direction"),
                consensus.get("label"),
            )

            return result, True

        except Exception as exc:
            error_message = (
                f"Signal evaluation failed for {symbol}: "
                f"{type(exc).__name__}: {exc}"
            )

            logger.exception("[LiveSignalEngine] %s", error_message)

            error_eval_iso = now_ist_iso()
            error_freshness = self._compute_freshness(
                candle_end,
                self._max_signal_age_seconds,
                now_ist_naive(),
            )

            error_result = {
                "symbol": symbol,
                "token": self._token_for(symbol),
                "ltp": None,
                "ltp_source": "ERROR",
                "ltp_timestamp": None,
                "candle_close": float(candle_dict["close"]),
                "candle_timestamp": source_candle_iso,
                "vwap": float(vwap) if self._valid_price(vwap) else None,
                "predictions": {},
                "consensus": self._empty_consensus("ERROR"),
                "timestamp": error_eval_iso,
                "data_source": "KITE_STREAM",
                "status": "ERROR",
                "error": error_message,
            }
            error_result.update(
                self._stamp(
                    symbol,
                    None,
                    source_candle_iso,
                    error_eval_iso,
                    error_freshness,
                )
            )

            try:
                from backend.streaming.market_stream_manager import (
                    market_stream_manager,
                )

                vp_facts = market_stream_manager.get_volume_profile(symbol)
                error_result["volume_profile"] = vp_facts.to_dict()
            except Exception as vp_exc:
                logger.debug(
                    "[LiveSignalEngine] Volume profile unavailable for %s: %s",
                    symbol,
                    vp_exc,
                )

            self._publish_candle_result(symbol, error_result, candle_timestamp)

            return error_result, False

    def _guard_ssf_signal(
        self,
        symbol: str,
        token: int,
        signal: Any,
        prediction: Any,
        ltp: float,
        snapshot_ts: datetime,
    ) -> Tuple[Any, Optional[Dict[str, Any]]]:
        if signal.action not in (SignalAction.BUY, SignalAction.SELL):
            return prediction, None

        direction = "LONG" if signal.action == SignalAction.BUY else "SHORT"
        trading_date = snapshot_ts.strftime("%Y-%m-%d")
        signal_id = f"SIG_SSF_{symbol}_{snapshot_ts.strftime('%Y%m%d_%H%M%S')}"
        block_reason: Optional[str] = None
        persisted = False

        state = self.signal_closed_state(symbol, _SSF_STRATEGY, trading_date)
        if state == _CLOSED:
            block_reason = (
                "SSF_L5_SRM signal concluded for this session: "
                + (
                    self.get_closed_reason(symbol, _SSF_STRATEGY, trading_date)
                    or "Session signal already resolved"
                )
            )
        elif state == _DB_ERROR:
            block_reason = (
                "SSF_L5_SRM signal state is unknown because the journal "
                "database is unavailable; signal withheld"
            )

        if block_reason is None:
            try:
                entry = float(signal.entry_price or ltp)
                stop = float(signal.stop_loss or 0.0)
                target = float(signal.target or 0.0)
            except (TypeError, ValueError):
                entry = stop = target = 0.0

            if not (
                self._valid_price(entry)
                and self._valid_price(stop)
                and self._valid_price(target)
            ):
                logger.error(
                    "[LiveSignalEngine] SSF signal %s has invalid entry/stop/target and cannot be journaled",
                    signal_id,
                )
                if self._require_persistence:
                    block_reason = (
                        "SSF_L5_SRM signal lacks valid entry, stop and target "
                        "and cannot be journaled; signal withheld"
                    )
            else:
                record = SignalEventRecord(
                    signal_id=signal_id,
                    trading_date=trading_date,
                    symbol=symbol,
                    instrument_token=token,
                    strategy=_SSF_STRATEGY,
                    direction=direction,
                    generated_at=now_ist_naive(),
                    candle_timestamp=None,
                    entry_price=round(entry, 2),
                    stop_loss=round(stop, 2),
                    target=round(target, 2),
                    notes=signal.reason or "SSF Level-5 book entry",
                )
                persisted = self._persist_signal(record) != _PERSIST_FAILED
                if not persisted:
                    logger.error(
                        "[LiveSignalEngine] Signal %s generated but NOT persisted",
                        signal_id,
                    )
                    if self._require_persistence:
                        block_reason = (
                            "SSF_L5_SRM signal could not be written to the "
                            "journal; signal withheld"
                        )

        if block_reason is not None:
            logger.warning(
                "[LiveSignalEngine] Suppressed SSF signal for %s: %s",
                symbol,
                block_reason,
            )
            return (
                self._suppressed_prediction(
                    symbol,
                    _SSF_STRATEGY,
                    prediction,
                    block_reason,
                ),
                {
                    "signal_id": signal_id,
                    "generated": True,
                    "persisted": persisted,
                    "published": False,
                },
            )

        return prediction, {
            "signal_id": signal_id,
            "generated": True,
            "persisted": persisted,
            "published": True,
        }

    def on_book_update(
        self,
        symbol: str,
        snapshot: Any,
    ) -> None:
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

        age = (now_ist_naive() - snapshot_ts).total_seconds()

        if age < 0 or age > self._max_ltp_age_seconds:
            logger.warning(
                "[LiveSignalEngine] Rejecting stale L5 snapshot for %s: age=%.1fs",
                clean,
                age,
            )
            return

        token = self._token_for(clean)
        ltp = snapshot.ltp

        if not self._valid_price(ltp):
            return

        if token is None or not isinstance(token, int) or token <= 0:
            logger.warning(
                "[LiveSignalEngine] No valid Kite token for SSF update: %s",
                clean,
            )
            return

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
        snapshot_iso = snapshot_ts.isoformat()

        try:
            strategy = self._ssf_runtime.get_strategy(
                symbol=clean,
                token=token,
                current_price=float(ltp),
            )
            signal = strategy.on_book_update(enriched)
            ssf_lifecycle: Optional[Dict[str, Any]] = None

            if signal is None:
                ssf_prediction = SingleStrategyPrediction(
                    status="WAITING",
                    reason="Current Level-5 conditions do not qualify.",
                    levels=getattr(strategy, "last_features", {}) or {},
                    metrics={},
                    strategy=_SSF_STRATEGY,
                    symbol=clean,
                )
            else:
                ssf_prediction = prediction_service._prediction_from_signal(
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
                    levels=getattr(strategy, "last_features", {}) or {},
                )
                ssf_prediction, ssf_lifecycle = self._guard_ssf_signal(
                    clean,
                    token,
                    signal,
                    ssf_prediction,
                    float(ltp),
                    snapshot_ts,
                )

            with self._lock:
                prior_payload = self._predictions.get(clean)
                if isinstance(prior_payload, dict):
                    prior_per_strategy = dict(prior_payload.get("predictions", {}))
                else:
                    prior_per_strategy = {}

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
                prior_per_strategy[_SSF_STRATEGY] = ssf_prediction

                consensus = prediction_service.calculate_consensus(
                    prior_per_strategy
                )

            evaluation_dt = now_ist_naive()
            evaluation_iso = now_ist_iso()
            has_prior = isinstance(prior_payload, dict)

            top_freshness = self._refresh_freshness_block(
                prior_payload.get("data_freshness") if has_prior else None,
                evaluation_dt,
            )

            predictions_out = {
                key: self._prediction_to_dict(value)
                for key, value in prior_per_strategy.items()
            }
            ssf_entry = predictions_out.get(_SSF_STRATEGY)
            if isinstance(ssf_entry, dict):
                ssf_entry.update(
                    self._stamp(
                        clean,
                        snapshot_iso if signal is not None else None,
                        None,
                        evaluation_iso,
                        self._compute_freshness(
                            snapshot_ts,
                            self._max_ltp_age_seconds,
                            evaluation_dt,
                        ),
                    )
                )
                ssf_entry["source_book_time"] = snapshot_iso
                if ssf_lifecycle is not None:
                    ssf_entry["signal_id"] = ssf_lifecycle["signal_id"]
                    ssf_entry["lifecycle"] = dict(ssf_lifecycle)

            result = {
                "symbol": clean,
                "token": token,
                "ltp": round(float(ltp), 2),
                "ltp_source": "L5_STREAM",
                "ltp_timestamp": ltp_timestamp.isoformat(),
                "candle_close": (
                    prior_payload.get("candle_close") if has_prior else None
                ),
                "candle_timestamp": (
                    prior_payload.get("candle_timestamp") if has_prior else None
                ),
                "vwap": prior_payload.get("vwap") if has_prior else None,
                "predictions": predictions_out,
                "consensus": consensus,
                "timestamp": evaluation_iso,
                "data_source": "KITE_STREAM",
            }
            result.update(
                self._stamp(
                    clean,
                    prior_payload.get("signal_time") if has_prior else None,
                    prior_payload.get("source_candle_time") if has_prior else None,
                    evaluation_iso,
                    top_freshness,
                )
            )

            self._store_result(clean, result)

        except Exception as exc:
            logger.exception(
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
                strategy=_SSF_STRATEGY,
                symbol=clean,
            )
            evaluation_dt = now_ist_naive()
            evaluation_iso = now_ist_iso()
            with self._lock:
                prior_payload = self._predictions.get(clean)
                if isinstance(prior_payload, dict):
                    prior_per_strategy = dict(prior_payload.get("predictions", {}))
                    prior_per_strategy.pop("SSF-L5-SRM", None)
                    prior_per_strategy[_SSF_STRATEGY] = ssf_prediction
                    consensus = prediction_service.calculate_consensus(
                        prior_per_strategy
                    )
                    predictions_out = {
                        k: self._prediction_to_dict(v)
                        for k, v in prior_per_strategy.items()
                    }
                    ssf_entry = predictions_out.get(_SSF_STRATEGY)
                    if isinstance(ssf_entry, dict):
                        ssf_entry.update(
                            self._stamp(
                                clean,
                                None,
                                None,
                                evaluation_iso,
                                self._compute_freshness(
                                    None,
                                    self._max_ltp_age_seconds,
                                    evaluation_dt,
                                ),
                            )
                        )
                    updated_payload = dict(prior_payload)
                    updated_payload["predictions"] = predictions_out
                    updated_payload["consensus"] = consensus
                    self._store_result(clean, updated_payload)

    def on_tick(
        self,
        symbol: str,
        price: float,
        timestamp: datetime,
    ) -> None:
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
            if status in {
                "UNAVAILABLE",
                "ERROR",
                "NO_TRADE",
                "WAITING",
                "STOPPED_OUT",
                "TARGET_HIT",
            }:
                continue
            if direction not in _VALID_DIRECTIONS:
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
                ts_date = (
                    timestamp.strftime("%Y-%m-%d")
                    if hasattr(timestamp, "strftime")
                    else now_ist_naive().strftime("%Y-%m-%d")
                )
                self.mark_signal_closed(clean, strat_name, ts_date, exit_reason)

        result = dict(payload)
        result["ltp"] = round(float(price), 2)
        result["ltp_timestamp"] = (
            timestamp.isoformat()
            if hasattr(timestamp, "isoformat")
            else str(timestamp)
        )
        if updated:
            result["predictions"] = predictions
            result["consensus"] = prediction_service.calculate_consensus(
                predictions
            )

        with self._lock:
            if self._predictions.get(clean) is not payload:
                return
            self._predictions[clean] = result

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

    def get_prediction(
        self,
        symbol: str,
    ) -> Optional[Dict[str, Any]]:
        normalized_symbol = self._normalize_symbol(symbol)

        if normalized_symbol is None:
            return None

        with self._lock:
            prediction = self._predictions.get(normalized_symbol)
            snapshot = dict(prediction) if prediction is not None else None

        return (
            self._with_current_freshness(snapshot)
            if snapshot is not None
            else None
        )

    def get_all_predictions(
        self,
    ) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            snapshots = {
                symbol: dict(payload)
                for symbol, payload in self._predictions.items()
            }
        return {
            symbol: self._with_current_freshness(payload)
            for symbol, payload in snapshots.items()
        }

    def get_live_signals(
        self,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            snapshots = [dict(payload) for payload in self._predictions.values()]
        return [self._with_current_freshness(payload) for payload in snapshots]

    def reset(self) -> None:
        with self._lock:
            self._predictions.clear()
            self._latest_ltp.clear()
            self._candle_ids.clear()
            self._last_candle_time.clear()
            self._inflight_candles.clear()
            self._closed_signals_today.clear()
        self._ssf_runtime.reset()
        self._crsd_runtime.reset()
        ssf_context_store.reset()


live_signal_engine = LiveSignalEngine()