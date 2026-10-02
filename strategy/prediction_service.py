
"""
Unified live strategy prediction service.

Responsibilities
----------------
- Prepare only completed real 15-minute candles for live evaluation.
- Preserve exchange/live VWAP when the upstream feed provides it.
- Build instrument configuration from the actual market price.
- Execute the repository's real strategy implementations.
- Isolate strategy failures so one broken strategy cannot fabricate or suppress
  the other strategies' real results.
- Validate every directional signal before exposing it.
- Compute transparent consensus using only live-enabled strategies.
- Never place, modify, cancel, or simulate broker orders.

This module is a signal/decision-support layer only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config.settings import AppSettings, InstrumentConfig, settings
from config.universe import create_instrument_config_for_equity
from data.market_calendar import MarketCalendar
from data.time_utils import now_ist_naive
from indicators.vwap import calculate_session_vwap
from monitoring.logger import logger
from strategy.apex_engine import ApexStrategy
from strategy.base_strategy import SignalAction, StrategySignal
from strategy.cpr_strategy import CPRRegimeBreakoutStrategy, Regime
from strategy.dual_ema_strategy import BufferedDualEMAStrategy
from strategy.aou_oss_strategy import AouOssStrategy
from strategy.orb_strategy import IntradayORBStrategy
from strategy.sector_impulse_strategy import SectorImpulseStrategy
from strategy.ssf_l5_srm_strategy import SsfL5SrmStrategy


STRATEGY_KEYS = (
    "orb",
    "cpr",
    "dual_ema",
    "apex",
    "sector_impulse",
    "ssf_l5_srm",
    "aou_oss",
)

# These are the only strategies allowed to contribute to live consensus
# in the current architecture. The other two remain visible but do not vote.
LIVE_CONSENSUS_STRATEGIES = (
    "orb",
    "cpr",
    "dual_ema",
    "apex",
)

REQUIRED_CANDLE_COLUMNS = {
    "datetime",
    "open",
    "high",
    "low",
    "close",
}

IST = "Asia/Kolkata"
SESSION_OPEN = dt_time(9, 15)
SESSION_CLOSE = dt_time(15, 30)
DEFAULT_TIMEFRAME_MINUTES = 15


def _finite_number(value: Any) -> Optional[float]:
    """Return a finite float or None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(number):
        return None

    return number


def _positive_number(value: Any) -> Optional[float]:
    number = _finite_number(value)
    if number is None or number <= 0:
        return None
    return number


def _normalize_ist_naive(value: Any) -> Optional[datetime]:
    """
    Normalize a timestamp to Asia/Kolkata and return naive IST datetime.

    Naive timestamps are treated as already-IST because the repository's
    strategy layer uses naive IST datetimes by contract.
    """
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
            ts = ts.tz_convert(IST).tz_localize(None)
    except (TypeError, ValueError):
        return None

    return ts.to_pydatetime()


def _safe_scalar(value: Any) -> Any:
    """
    Recursively normalize common numpy/pandas values into JSON-safe scalars.
    """
    if value is None:
        return None

    if isinstance(value, (np.integer,)):
        return int(value)

    if isinstance(value, (np.floating,)):
        value = float(value)
        return value if math.isfinite(value) else None

    if isinstance(value, (np.bool_,)):
        return bool(value)

    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()

    if isinstance(value, float):
        return value if math.isfinite(value) else None

    if isinstance(value, (str, int, bool)):
        return value

    if isinstance(value, dict):
        return {
            str(k): _safe_scalar(v)
            for k, v in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [_safe_scalar(v) for v in value]

    return value


@dataclass
class SingleStrategyPrediction:
    """
    Public prediction object exposed to backend/frontend.
    """

    status: str
    direction: Optional[str] = None
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    reason: str = ""
    levels: Optional[Dict[str, Any]] = None
    metrics: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "status": self.status,
        }

        if self.direction is not None:
            payload["direction"] = self.direction

        if self.entry is not None:
            payload["entry"] = round(float(self.entry), 2)

        if self.stop_loss is not None:
            payload["stop_loss"] = round(float(self.stop_loss), 2)

        if self.target is not None:
            payload["target"] = round(float(self.target), 2)

        if self.reason:
            payload["reason"] = str(self.reason)

        payload["levels"] = _safe_scalar(
            self.levels or {}
        )
        payload["metrics"] = _safe_scalar(
            self.metrics or {}
        )

        return payload


@dataclass
class CandidatePrediction:
    rank: int
    symbol: str
    ltp: float
    momentum_score: float
    universe_bias: str
    predictions: Dict[str, SingleStrategyPrediction]
    consensus: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        predictions = {
            key: (
                value.to_dict()
                if isinstance(
                    value,
                    SingleStrategyPrediction,
                )
                else _safe_scalar(value)
            )
            for key, value in self.predictions.items()
        }

        return {
            "rank": int(self.rank),
            "symbol": self.symbol,
            "ltp": round(float(self.ltp), 2),
            "momentum_score": round(
                float(self.momentum_score),
                1,
            ),
            "universe_bias": self.universe_bias,
            "predictions": predictions,
            # Kept for frontend compatibility.
            "strategies": predictions,
            "consensus": _safe_scalar(
                self.consensus
            ),
        }


class PredictionService:
    """
    Single source of truth for strategy evaluation and consensus.

    The service never manufactures a trading value. A missing required input
    results in NO_TRADE, UNAVAILABLE, or ERROR.
    """

    LIVE_CONSENSUS_STRATEGIES = LIVE_CONSENSUS_STRATEGIES

    def __init__(
        self,
        app_settings: AppSettings = settings,
    ):
        self.settings = app_settings
        self.cache_dir = (
            self.settings.base_dir
            / "data"
            / "cache"
        )
        self.cache_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

    # ================================================================
    # INPUT VALIDATION / PREPARATION
    # ================================================================

    @staticmethod
    def _latest_completed_15m_start(
        now_ist: datetime,
    ) -> Optional[datetime]:
        """
        Return the open timestamp of the latest fully completed 15m candle.

        At 10:07 -> 09:45
        At 10:15 -> 10:00
        At 15:30 -> 15:15
        Before 09:30 -> None
        """
        now = _normalize_ist_naive(now_ist)

        if now is None:
            return None

        session_start = datetime.combine(
            now.date(),
            SESSION_OPEN,
        )
        session_close = datetime.combine(
            now.date(),
            SESSION_CLOSE,
        )

        first_completed = (
            session_start
            + timedelta(
                minutes=DEFAULT_TIMEFRAME_MINUTES
            )
        )

        if now < first_completed:
            return None

        effective_now = min(
            now,
            session_close,
        )

        elapsed_minutes = (
            effective_now - session_start
        ).total_seconds() / 60.0

        completed_bars = int(
            elapsed_minutes
            // DEFAULT_TIMEFRAME_MINUTES
        )

        if completed_bars <= 0:
            return None

        return session_start + timedelta(
            minutes=(
                completed_bars - 1
            ) * DEFAULT_TIMEFRAME_MINUTES
        )

    @staticmethod
    def _validate_candle_dataframe(
        data: pd.DataFrame,
    ) -> bool:
        if data is None or data.empty:
            return False

        if not REQUIRED_CANDLE_COLUMNS.issubset(
            data.columns
        ):
            return False

        numeric_columns = [
            "open",
            "high",
            "low",
            "close",
        ]

        for column in numeric_columns:
            data[column] = pd.to_numeric(
                data[column],
                errors="coerce",
            )

        if "volume" in data.columns:
            data["volume"] = pd.to_numeric(
                data["volume"],
                errors="coerce",
            ).fillna(0.0)

        valid = (
            data["datetime"].notna()
            & data["open"].notna()
            & data["high"].notna()
            & data["low"].notna()
            & data["close"].notna()
            & (data["open"] > 0)
            & (data["high"] > 0)
            & (data["low"] > 0)
            & (data["close"] > 0)
            & (data["high"] >= data["low"])
            & (data["high"] >= data["open"])
            & (data["high"] >= data["close"])
            & (data["low"] <= data["open"])
            & (data["low"] <= data["close"])
        )

        return bool(valid.all())

    def _prepare_live_candles(
        self,
        df_15m: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Prepare real completed 15-minute candles for live strategy evaluation.

        Keeps historical sessions because CPR, Dual EMA, APEX and other
        strategies may require prior-session context.

        Rules:
        - Keep all valid historical candles supplied by the caller.
        - Normalize timestamps to IST.
        - Remove invalid candles.
        - Remove duplicate timestamps.
        - Never include a forming/future candle.
        - Do not manufacture missing candles.
        - Do not restrict the dataset to today's session.
        """
        if (
            df_15m is None
            or not isinstance(df_15m, pd.DataFrame)
            or df_15m.empty
        ):
            return pd.DataFrame()

        data = df_15m.copy()

        if not REQUIRED_CANDLE_COLUMNS.issubset(data.columns):
            return pd.DataFrame()

        # Normalize every timestamp to IST.
        data["datetime"] = data["datetime"].map(
            _normalize_ist_naive
        )

        data = data.dropna(
            subset=["datetime"]
        ).copy()

        if data.empty:
            return pd.DataFrame()

        # Validate real OHLC data.
        if not self._validate_candle_dataframe(data):
            return pd.DataFrame()

        # Chronological order is mandatory for all strategy calculations.
        data = data.sort_values(
            "datetime"
        ).reset_index(drop=True)

        # One timestamp = one source candle.
        data = data.drop_duplicates(
            subset=["datetime"],
            keep="last",
        ).reset_index(drop=True)

        now_ist = now_ist_naive()

        # Never allow future candles.
        data = data[
            data["datetime"] <= now_ist
        ].copy()

        if data.empty:
            return pd.DataFrame()

        # Do not allow today's forming 15-minute candle.
        latest_completed = self._latest_completed_15m_start(
            now_ist
        )

        today = now_ist.date()

        # Historical sessions remain untouched.
        # Only today's candles are restricted to completed bars.
        historical = data[
            data["datetime"].dt.date < today
        ]

        if latest_completed is not None:
            today_completed = data[
                (data["datetime"].dt.date == today)
                & (
                    data["datetime"]
                    <= latest_completed
                )
            ]
        else:
            today_completed = data.iloc[0:0]

        data = pd.concat(
            [
                historical,
                today_completed,
            ],
            ignore_index=True,
        )

        data = data.sort_values(
            "datetime"
        ).reset_index(drop=True)

        return data

    def _prepare_data(
        self,
        df_15m: pd.DataFrame,
    ) -> Tuple[
        pd.DataFrame,
        List[Tuple[date, pd.DataFrame]],
    ]:
        """
        Prepare chronological strategy data.

        Existing valid VWAP values are preserved. Missing VWAP values are
        derived from the real OHLCV candles as an explicit fallback.
        """
        if (
            df_15m is None
            or not isinstance(df_15m, pd.DataFrame)
            or df_15m.empty
        ):
            return pd.DataFrame(), []

        data = df_15m.copy()

        if (
            "datetime" not in data.columns
            and isinstance(
                data.index,
                pd.DatetimeIndex,
            )
        ):
            data["datetime"] = data.index

        if "datetime" not in data.columns:
            return pd.DataFrame(), []

        data["datetime"] = data["datetime"].map(
            _normalize_ist_naive
        )

        data = data.dropna(
            subset=["datetime"]
        ).copy()

        required = {
            "datetime",
            "open",
            "high",
            "low",
            "close",
        }

        if not required.issubset(
            data.columns
        ):
            return pd.DataFrame(), []

        for column in (
            "open",
            "high",
            "low",
            "close",
        ):
            data[column] = pd.to_numeric(
                data[column],
                errors="coerce",
            )

        if "volume" not in data.columns:
            data["volume"] = 0.0

        data["volume"] = pd.to_numeric(
            data["volume"],
            errors="coerce",
        ).fillna(0.0)

        data = data.dropna(
            subset=[
                "open",
                "high",
                "low",
                "close",
            ]
        ).copy()

        data = data[
            (data["open"] > 0)
            & (data["high"] > 0)
            & (data["low"] > 0)
            & (data["close"] > 0)
            & (data["high"] >= data["low"])
            & (data["high"] >= data["open"])
            & (data["high"] >= data["close"])
            & (data["low"] <= data["open"])
            & (data["low"] <= data["close"])
        ].copy()

        data = data.sort_values(
            "datetime"
        )

        data = data.drop_duplicates(
            subset=["datetime"],
            keep="last",
        ).reset_index(drop=True)

        if data.empty:
            return pd.DataFrame(), []

        derived_vwap = calculate_session_vwap(
            data
        )

        if (
            "vwap" not in data.columns
        ):
            data["vwap"] = derived_vwap.to_numpy()
        else:
            supplied_vwap = pd.to_numeric(
                data["vwap"],
                errors="coerce",
            )

            use_derived = (
                supplied_vwap.isna()
                | ~np.isfinite(
                    supplied_vwap.to_numpy()
                )
                | (supplied_vwap <= 0)
            )

            data["vwap"] = supplied_vwap

            data.loc[
                use_derived,
                "vwap",
            ] = derived_vwap.loc[
                data.index[
                    use_derived
                ]
            ].to_numpy()

        data["date"] = (
            data["datetime"].dt.date
        )

        days = list(
            data.groupby(
                "date",
                sort=True,
            )
        )

        return data, days

    @staticmethod
    def _resolve_ltp(
        df_15m: pd.DataFrame,
        current_ltp: Optional[float],
    ) -> Optional[float]:
        """
        Use the actual supplied live LTP when valid.

        When the caller does not provide one, the latest completed candle
        close is used as a derived fallback from real market data. No constant
        price is ever introduced.
        """
        supplied = _positive_number(
            current_ltp
        )

        if supplied is not None:
            return supplied

        if (
            df_15m is None
            or df_15m.empty
            or "close" not in df_15m.columns
        ):
            return None

        return _positive_number(
            df_15m["close"].iloc[-1]
        )

    @staticmethod
    def _candle_dict(
        row: pd.Series,
    ) -> Dict[str, Any]:
        return {
            "datetime": row["datetime"],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": max(
                int(row.get("volume", 0)),
                0,
            ),
        }

    # ================================================================
    # SIGNAL VALIDATION
    # ================================================================

    @staticmethod
    def _validate_directional_signal(
        signal: Optional[StrategySignal],
        ltp: float,
    ) -> Optional[str]:
        """
        Validate the economic geometry of a strategy signal.

        LONG:
            stop < entry < target

        SHORT:
            target < entry < stop

        Also require positive finite values and a non-expired current LTP.
        """
        if signal is None:
            return "NO_SIGNAL"

        if signal.action not in {
            SignalAction.BUY,
            SignalAction.SELL,
        }:
            return "NON_DIRECTIONAL"

        entry = _positive_number(
            getattr(signal, "price", None)
        )

        stop = _positive_number(
            getattr(signal, "stop_loss", None)
        )

        target = _positive_number(
            getattr(signal, "target", None)
        )

        if (
            entry is None
            or stop is None
            or target is None
        ):
            return (
                "INVALID_SIGNAL_LEVELS"
            )

        direction = (
            "LONG"
            if signal.action == SignalAction.BUY
            else "SHORT"
        )

        if direction == "LONG":
            if not (
                stop < entry < target
            ):
                return (
                    "INVALID_LONG_LEVEL_ORDER"
                )

            if ltp <= stop:
                return (
                    "EXPIRED_LONG_STOP"
                )

            if ltp >= target:
                return (
                    "EXPIRED_LONG_TARGET"
                )

        else:
            if not (
                target < entry < stop
            ):
                return (
                    "INVALID_SHORT_LEVEL_ORDER"
                )

            if ltp >= stop:
                return (
                    "EXPIRED_SHORT_STOP"
                )

            if ltp <= target:
                return (
                    "EXPIRED_SHORT_TARGET"
                )

        return None

    @staticmethod
    def _prediction_from_signal(
        status: str,
        signal: StrategySignal,
        ltp: float,
        default_reason: str,
        levels: Optional[Dict[str, Any]] = None,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> SingleStrategyPrediction:
        validation_error = (
            PredictionService
            ._validate_directional_signal(
                signal,
                ltp,
            )
        )

        if validation_error is not None:
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason=(
                    f"Signal rejected: "
                    f"{validation_error}."
                ),
                levels=levels or {},
                metrics=metrics or {},
            )

        direction = (
            "LONG"
            if signal.action == SignalAction.BUY
            else "SHORT"
        )

        return SingleStrategyPrediction(
            status=status,
            direction=direction,
            entry=float(signal.price),
            stop_loss=float(
                signal.stop_loss
            ),
            target=float(
                signal.target
            ),
            reason=(
                signal.reason
                or default_reason
            ),
            levels=levels or {},
            metrics=metrics or {},
        )

    @staticmethod
    def _error_prediction(
        strategy_name: str,
        exc: Exception,
    ) -> SingleStrategyPrediction:
        """
        Convert a strategy exception into an explicit ERROR state.

        ERROR never carries direction/entry/SL/target, therefore it cannot
        accidentally vote in consensus.
        """
        return SingleStrategyPrediction(
            status="ERROR",
            reason=(
                f"{strategy_name} evaluation failed: "
                f"{type(exc).__name__}: {exc}"
            ),
            levels={},
            metrics={},
        )

    # ================================================================
    # PUBLIC EVALUATION
    # ================================================================

    def evaluate_symbol(
        self,
        symbol: str,
        df_15m: pd.DataFrame,
        current_ltp: Optional[float] = None,
        token: Optional[int] = None,
        stock_metric: Optional[Any] = None,
        book_snapshot: Optional[Any] = None,
        peer_context: Optional[Any] = None,
        kite_client: Optional[Any] = None,
        ssf_strategy: Optional[Any] = None,
    ) -> Tuple[
        Dict[str, SingleStrategyPrediction],
        Dict[str, Any],
    ]:
        """
        Evaluate all six strategies independently.

        One strategy failure never suppresses all other valid strategies.
        """
        clean_symbol = (
            str(symbol).strip().upper()
        )

        live_df = self._prepare_live_candles(
            df_15m
        )

        if live_df.empty:
            predictions = {
                key: SingleStrategyPrediction(
                    status="UNAVAILABLE",
                    reason=(
                        "No completed 15-minute candle "
                        "data is available for live evaluation."
                    ),
                    levels={},
                    metrics={},
                )
                for key in STRATEGY_KEYS
            }

            return (
                predictions,
                self.calculate_consensus(
                    predictions
                ),
            )

        ltp = self._resolve_ltp(
            live_df,
            current_ltp,
        )

        if ltp is None:
            predictions = {
                key: SingleStrategyPrediction(
                    status="UNAVAILABLE",
                    reason=(
                        "No valid real market LTP "
                        "was supplied and no valid "
                        "completed candle close exists."
                    ),
                    levels={},
                    metrics={},
                )
                for key in STRATEGY_KEYS
            }

            return (
                predictions,
                self.calculate_consensus(
                    predictions
                ),
            )

        # Critical fix:
        # the actual market price is passed into the equity configuration.
        inst = create_instrument_config_for_equity(
            clean_symbol,
            token or 0,
            current_price=ltp,
        )

        predictions: Dict[
            str,
            SingleStrategyPrediction,
        ] = {}

        # Each strategy is isolated.
        # A failure becomes ERROR, not fake data and not a whole-request failure.
        try:
            predictions["orb"] = (
                self._evaluate_orb(
                    inst,
                    live_df,
                    ltp,
                )
            )
        except Exception as exc:
            predictions["orb"] = (
                self._error_prediction(
                    "ORB",
                    exc,
                )
            )

        try:
            predictions["cpr"] = (
                self._evaluate_cpr(
                    inst,
                    live_df,
                    ltp,
                )
            )
        except Exception as exc:
            predictions["cpr"] = (
                self._error_prediction(
                    "CPR",
                    exc,
                )
            )

        try:
            predictions["dual_ema"] = (
                self._evaluate_dual_ema(
                    inst,
                    live_df,
                    ltp,
                )
            )
        except Exception as exc:
            predictions["dual_ema"] = (
                self._error_prediction(
                    "Dual EMA",
                    exc,
                )
            )

        try:
            predictions["apex"] = (
                self._evaluate_apex(
                    inst,
                    live_df,
                    ltp,
                )
            )
        except Exception as exc:
            predictions["apex"] = (
                self._error_prediction(
                    "APEX",
                    exc,
                )
            )

        try:
            predictions["sector_impulse"] = (
                self._evaluate_sector_impulse(
                    inst,
                    live_df,
                    ltp,
                    peer_context=peer_context,
                    kite_client=kite_client,
                )
            )
        except Exception as exc:
            predictions["sector_impulse"] = (
                self._error_prediction(
                    "Sector Impulse",
                    exc,
                )
            )

        try:
            predictions["ssf_l5_srm"] = (
                self._evaluate_ssf_l5_srm(
                    inst,
                    live_df,
                    ltp,
                    book_snapshot=book_snapshot,
                    ssf_strategy=ssf_strategy,
                )
            )
        except Exception as exc:
            predictions["ssf_l5_srm"] = (
                self._error_prediction(
                    "SSF-L5-SRM",
                    exc,
                )
            )

        try:
            predictions["aou_oss"] = (
                self._evaluate_aou_oss(
                    inst,
                    live_df,
                    ltp,
                    book_snapshot=book_snapshot,
                )
            )
        except Exception as exc:
            predictions["aou_oss"] = (
                self._error_prediction(
                    "AOU-OSS",
                    exc,
                )
            )

        consensus = self.calculate_consensus(
            predictions
        )

        return predictions, consensus

    # ================================================================
    # ORB
    # ================================================================

    def _evaluate_orb(
        self,
        inst: InstrumentConfig,
        df_15m: pd.DataFrame,
        ltp: float,
    ) -> SingleStrategyPrediction:
        if df_15m.empty:
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason="No 15m candle data available.",
            )

        _, days = self._prepare_data(
            df_15m
        )

        if not days:
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason="No trading days found.",
            )

        latest_date, today_df = days[-1]

        strategy = IntradayORBStrategy(
            inst,
            self.settings.strategy,
        )

        strategy.reset_session(
            latest_date
        )

        current_signal: Optional[
            StrategySignal
        ] = None

        for _, row in today_df.iterrows():
            candle = self._candle_dict(
                row
            )

            vwap = _positive_number(
                row.get("vwap")
            )

            if vwap is None:
                continue

            current_signal = strategy.on_candle(
                candle,
                vwap,
            )

        orb_info: Dict[str, Any] = {}

        if strategy.orb is not None:
            orb_info = {
                "orb_high": round(
                    float(strategy.orb.high),
                    2,
                ),
                "orb_low": round(
                    float(strategy.orb.low),
                    2,
                ),
                "orb_width": round(
                    float(strategy.orb.width),
                    2,
                ),
                "is_valid_volatility": bool(
                    strategy.orb.is_valid_volatility
                ),
            }

        if current_signal is not None:
            if current_signal.action == SignalAction.BUY:
                return self._prediction_from_signal(
                    status="LONG_BREAKOUT",
                    signal=current_signal,
                    ltp=ltp,
                    default_reason=(
                        "Price broke above the "
                        "30-minute ORB High with "
                        "VWAP confirmation."
                    ),
                    levels=orb_info,
                )

            if current_signal.action == SignalAction.SELL:
                return self._prediction_from_signal(
                    status="SHORT_BREAKDOWN",
                    signal=current_signal,
                    ltp=ltp,
                    default_reason=(
                        "Price broke below the "
                        "30-minute ORB Low with "
                        "VWAP confirmation."
                    ),
                    levels=orb_info,
                )

        if strategy.orb is None:
            return SingleStrategyPrediction(
                status="WAITING",
                reason=(
                    "Establishing the 30-minute "
                    "opening range (09:15-09:45 IST)."
                ),
                levels=orb_info,
            )

        if not strategy.orb.is_valid_volatility:
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason=(
                    "Opening range failed the "
                    "configured volatility filter."
                ),
                levels=orb_info,
            )

        return SingleStrategyPrediction(
            status="NO_TRADE",
            reason=(
                "No valid ORB breakout signal "
                "on the latest completed candle."
            ),
            levels=orb_info,
        )

    # ================================================================
    # CPR
    # ================================================================

    def _evaluate_cpr(
        self,
        inst: InstrumentConfig,
        df_15m: pd.DataFrame,
        ltp: float,
    ) -> SingleStrategyPrediction:
        if df_15m.empty:
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason="No 15m candle data available.",
            )

        _, days = self._prepare_data(
            df_15m
        )

        if len(days) < 2:
            return SingleStrategyPrediction(
                status="UNAVAILABLE",
                reason=(
                    "CPR cannot evaluate: "
                    "prior-session data is required."
                ),
                levels={},
                metrics={},
            )

        lookback_df = pd.concat(
            [
                day_df
                for _, day_df in days[:-1]
            ],
            ignore_index=True,
        )

        latest_date, today_df = days[-1]

        strategy = CPRRegimeBreakoutStrategy(
            inst,
            self.settings.strategy,
        )

        strategy.seed_context(
            lookback_df
        )
        strategy.reset_session(
            latest_date
        )

        current_signal: Optional[
            StrategySignal
        ] = None

        for _, row in today_df.iterrows():
            candle = self._candle_dict(
                row
            )

            vwap = _positive_number(
                row.get("vwap")
            )

            if vwap is None:
                continue

            current_signal = strategy.on_candle(
                candle,
                vwap,
            )

        cpr_levels: Dict[str, Any] = {}

        if strategy.pivots:
            cpr_levels = {
                "pivot": round(
                    float(strategy.pivots["P"]),
                    2,
                ),
                "bottom_central": round(
                    float(strategy.pivots["BC"]),
                    2,
                ),
                "top_central": round(
                    float(strategy.pivots["TC"]),
                    2,
                ),
                "r1": round(
                    float(strategy.pivots["R1"]),
                    2,
                ),
                "s1": round(
                    float(strategy.pivots["S1"]),
                    2,
                ),
                "cpr_width_pct": round(
                    float(
                        strategy.pivots[
                            "width_pct"
                        ]
                    ),
                    2,
                ),
                "regime": _safe_scalar(
                    strategy.regime
                ),
            }

        if current_signal is not None:
            if current_signal.action == SignalAction.BUY:
                return self._prediction_from_signal(
                    status="BULLISH_EXPANSION",
                    signal=current_signal,
                    ltp=ltp,
                    default_reason=(
                        "Valid CPR bullish breakout "
                        "on the latest completed candle."
                    ),
                    levels=cpr_levels,
                )

            if current_signal.action == SignalAction.SELL:
                return self._prediction_from_signal(
                    status="BEARISH_EXPANSION",
                    signal=current_signal,
                    ltp=ltp,
                    default_reason=(
                        "Valid CPR bearish breakdown "
                        "on the latest completed candle."
                    ),
                    levels=cpr_levels,
                )

        if strategy.regime == Regime.NEUTRAL:
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason=(
                    "CPR regime is neutral; "
                    "no directional setup is active."
                ),
                levels=cpr_levels,
            )

        return SingleStrategyPrediction(
            status="NO_TRADE",
            reason=(
                "No valid CPR trigger on the "
                "latest completed candle."
            ),
            levels=cpr_levels,
        )

    # ================================================================
    # DUAL EMA
    # ================================================================

    def _evaluate_dual_ema(
        self,
        inst: InstrumentConfig,
        df_15m: pd.DataFrame,
        ltp: float,
    ) -> SingleStrategyPrediction:
        if df_15m.empty:
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason="No 15m candle data available.",
            )

        _, days = self._prepare_data(
            df_15m
        )

        if len(days) < 2:
            return SingleStrategyPrediction(
                status="UNAVAILABLE",
                reason=(
                    "Dual-EMA cannot evaluate: "
                    "prior-session history is required "
                    "for SMA200/ATR warm-up."
                ),
                levels={},
                metrics={},
            )

        lookback_df = pd.concat(
            [
                day_df
                for _, day_df in days[:-1]
            ],
            ignore_index=True,
        )

        latest_date, today_df = days[-1]

        strategy = BufferedDualEMAStrategy(
            inst,
            self.settings.strategy,
        )

        strategy.seed_context(
            lookback_df
        )
        strategy.reset_session(
            latest_date
        )

        current_signal: Optional[
            StrategySignal
        ] = None

        for _, row in today_df.iterrows():
            candle = self._candle_dict(
                row
            )

            vwap = _positive_number(
                row.get("vwap")
            )

            if vwap is None:
                continue

            current_signal = strategy.on_candle(
                candle,
                vwap,
            )

        levels: Dict[str, Any] = {}

        current_indicators, _ = (
            strategy.latest_indicators()
        )

        if current_indicators is not None:
            atr = _positive_number(
                current_indicators["atr14"]
            )

            levels = {
                "ema_fast": round(
                    float(
                        current_indicators[
                            "ema9"
                        ]
                    ),
                    2,
                ),
                "ema_slow": round(
                    float(
                        current_indicators[
                            "ema21"
                        ]
                    ),
                    2,
                ),
                "sma_trend": round(
                    float(
                        current_indicators[
                            "sma200"
                        ]
                    ),
                    2,
                ),
                "atr_14": round(
                    float(
                        current_indicators[
                            "atr14"
                        ]
                    ),
                    2,
                ),
                "buffer": round(
                    float(
                        strategy.buffer_gamma
                        * (
                            atr
                            if atr is not None
                            else 0.0
                        )
                    ),
                    2,
                ),
            }

        if current_signal is not None:
            if current_signal.action == SignalAction.BUY:
                return self._prediction_from_signal(
                    status="TRENDING_LONG",
                    signal=current_signal,
                    ltp=ltp,
                    default_reason=(
                        "Validated bullish Dual-EMA "
                        "buffer crossover with SMA200 "
                        "trend confirmation."
                    ),
                    levels=levels,
                )

            if current_signal.action == SignalAction.SELL:
                return self._prediction_from_signal(
                    status="TRENDING_SHORT",
                    signal=current_signal,
                    ltp=ltp,
                    default_reason=(
                        "Validated bearish Dual-EMA "
                        "buffer crossover with SMA200 "
                        "trend confirmation."
                    ),
                    levels=levels,
                )

        if current_indicators is None:
            return SingleStrategyPrediction(
                status="UNAVAILABLE",
                reason=(
                    "Insufficient valid history for "
                    "the required SMA200/ATR14 warm-up."
                ),
                levels=levels,
            )

        return SingleStrategyPrediction(
            status="NO_TRADE",
            reason=(
                "No valid Dual-EMA crossover "
                "signal on the latest completed candle."
            ),
            levels=levels,
        )

    # ================================================================
    # APEX
    # ================================================================

    def _evaluate_apex(
        self,
        inst: InstrumentConfig,
        df_15m: pd.DataFrame,
        ltp: float,
    ) -> SingleStrategyPrediction:
        if df_15m.empty:
            return SingleStrategyPrediction(
                status="UNAVAILABLE",
                reason=(
                    "APEX cannot evaluate: "
                    "no 15-minute candle data is available."
                ),
                levels={},
                metrics={},
            )

        _, days = self._prepare_data(
            df_15m
        )

        if not days:
            return SingleStrategyPrediction(
                status="UNAVAILABLE",
                reason=(
                    "APEX cannot evaluate: "
                    "no trading-session data is available."
                ),
                levels={},
                metrics={},
            )

        if len(days) < 2:
            return SingleStrategyPrediction(
                status="UNAVAILABLE",
                reason=(
                    "APEX cannot evaluate: "
                    "prior-session history is required "
                    "for prior ATR and opening-gap calculation."
                ),
                levels={},
                metrics={},
            )

        latest_date, today_df = days[-1]

        lookback_df = pd.concat(
            [
                day_df
                for _, day_df in days[:-1]
            ],
            ignore_index=True,
        )

        strategy = ApexStrategy(
            inst,
            self.settings.strategy,
        )

        strategy.seed_context(
            lookback_df
        )
        strategy.reset_session(
            latest_date
        )

        current_signal: Optional[
            StrategySignal
        ] = None

        for _, row in today_df.iterrows():
            candle = self._candle_dict(
                row
            )

            vwap = _positive_number(
                row.get("vwap")
            )

            if vwap is None:
                continue

            current_signal = strategy.on_candle(
                candle,
                vwap,
            )

        analysis = (
            getattr(
                strategy,
                "last_analysis",
                None,
            )
            or {}
        )

        levels = {
            key: value
            for key, value
            in analysis.items()
            if key not in {
                "status",
                "reason",
                "direction",
                "entry",
                "stop_loss",
                "target",
            }
        }

        if current_signal is not None:
            if current_signal.action == SignalAction.BUY:
                return self._prediction_from_signal(
                    status="APEX_LONG",
                    signal=current_signal,
                    ltp=ltp,
                    default_reason=(
                        "APEX generated a validated "
                        "bullish signal."
                    ),
                    levels=levels,
                )

            if current_signal.action == SignalAction.SELL:
                return self._prediction_from_signal(
                    status="APEX_SHORT",
                    signal=current_signal,
                    ltp=ltp,
                    default_reason=(
                        "APEX generated a validated "
                        "bearish signal."
                    ),
                    levels=levels,
                )

        status = str(
            analysis.get(
                "status",
                "NO_TRADE",
            )
        )

        reason = str(
            analysis.get(
                "reason",
                "APEX criteria not met.",
            )
        )

        if status in {
            "LONG",
            "SHORT",
            "BUY",
            "SELL",
        }:
            status = "NO_TRADE"

        return SingleStrategyPrediction(
            status=status,
            reason=reason,
            levels=levels,
        )

    # ================================================================
    # SECTOR IMPULSE
    # ================================================================

    def _evaluate_sector_impulse(
        self,
        inst: InstrumentConfig,
        df_15m: pd.DataFrame,
        ltp: float,
        peer_context: Optional[Any] = None,
        kite_client: Optional[Any] = None,
    ) -> SingleStrategyPrediction:
        if df_15m.empty:
            return SingleStrategyPrediction(
                status="UNAVAILABLE",
                reason=(
                    "Sector Impulse cannot evaluate: "
                    "no 15-minute candle data is available."
                ),
                levels={},
                metrics={},
            )

        _, days = self._prepare_data(
            df_15m
        )

        if not days:
            return SingleStrategyPrediction(
                status="UNAVAILABLE",
                reason=(
                    "Sector Impulse cannot evaluate: "
                    "no valid trading-session data found."
                ),
                levels={},
                metrics={},
            )

        latest_date, today_df = days[-1]

        lookback_df = (
            pd.concat(
                [
                    day_df
                    for _, day_df in days[:-1]
                ],
                ignore_index=True,
            )
            if len(days) > 1
            else pd.DataFrame()
        )

        ctx = peer_context

        if ctx is None:
            try:
                from data.sector_peer_manager import (
                    SectorPeerManager,
                )

                ctx = (
                    SectorPeerManager.build_peer_context(
                        symbol=inst.symbol,
                        cache_dir=self.cache_dir,
                        kite_client=kite_client,
                    )
                )
            except Exception as exc:
                logger.warning(
                    f"[{inst.symbol}] SectorPeerManager unavailable: {type(exc).__name__}: {exc}"
                )
                return SingleStrategyPrediction(
                    status="UNAVAILABLE",
                    reason=(
                        "Sector peer context unavailable: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    levels={},
                )

        if ctx is None:
            return SingleStrategyPrediction(
                status="UNAVAILABLE",
                reason=(
                    "Sector leader/index peer context "
                    "is unavailable."
                ),
                levels={},
            )

        strategy = SectorImpulseStrategy(
            inst,
            self.settings.strategy,
            ctx=ctx,
        )

        strategy.seed_context(
            lookback_df
        )
        strategy.reset_session(
            latest_date
        )

        current_signal: Optional[
            StrategySignal
        ] = None

        for _, row in today_df.iterrows():
            candle = self._candle_dict(
                row
            )

            vwap = _positive_number(
                row.get("vwap")
            )

            if vwap is None:
                continue

            current_signal = strategy.on_candle(
                candle,
                vwap,
            )

        model = getattr(
            strategy,
            "model",
            None,
        )

        levels: Dict[str, Any] = {}

        if model:
            rho = _finite_number(
                model.get("rho")
            )
            mkt_sig = _finite_number(
                model.get("mkt_sig")
            )

            levels = {
                "lead_lag_k": _safe_scalar(
                    model.get("k")
                ),
                "rho": (
                    round(rho, 3)
                    if rho is not None
                    else None
                ),
                "mkt_sig": (
                    round(mkt_sig, 4)
                    if mkt_sig is not None
                    else None
                ),
            }

        if current_signal is not None:
            if current_signal.action == SignalAction.BUY:
                return self._prediction_from_signal(
                    status="IMPULSE_LONG",
                    signal=current_signal,
                    ltp=ltp,
                    default_reason=(
                        "Sector impulse strategy "
                        "generated a validated LONG signal."
                    ),
                    levels=levels,
                )

            if current_signal.action == SignalAction.SELL:
                return self._prediction_from_signal(
                    status="IMPULSE_SHORT",
                    signal=current_signal,
                    ltp=ltp,
                    default_reason=(
                        "Sector impulse strategy "
                        "generated a validated SHORT signal."
                    ),
                    levels=levels,
                )

        if model is None:
            disabled_reason = getattr(
                strategy,
                "disabled_reason",
                None,
            )

            reason = (
                str(disabled_reason)
                if disabled_reason
                else (
                    "Sector impulse model is "
                    "not currently eligible."
                )
            )

            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason=reason,
                levels=levels,
            )

        rho = _finite_number(
            model.get("rho")
        )

        rho_text = (
            f"{rho:.2f}"
            if rho is not None
            else "N/A"
        )

        return SingleStrategyPrediction(
            status="MONITORING",
            reason=(
                "Monitoring sector impulse "
                f"transmission (k={model.get('k')}, "
                f"rho={rho_text})."
            ),
            levels=levels,
        )

    # ================================================================
    # SSF-L5-SRM
    # ================================================================

    def _evaluate_ssf_l5_srm(
        self,
        inst: InstrumentConfig,
        df_15m: pd.DataFrame,
        ltp: float,
        book_snapshot: Optional[Any] = None,
        ssf_strategy: Optional[Any] = None,
    ) -> SingleStrategyPrediction:
        if book_snapshot is None or len(getattr(book_snapshot, "bids", [])) < 5 or len(getattr(book_snapshot, "asks", [])) < 5:
            return SingleStrategyPrediction(
                status="UNAVAILABLE",
                reason=(
                    "SSF-L5-SRM requires a real Level-5 book snapshot."
                ),
                levels={},
                metrics={},
            )

        strategy = ssf_strategy
        if strategy is None:
            strategy = SsfL5SrmStrategy(
                inst,
                self.settings.strategy,
                signal_only=True,
            )

        regime_ok = bool(
            getattr(
                strategy,
                "regime_ok",
                False,
            )
        )

        bars = getattr(
            strategy,
            "_bars",
            [],
        )

        levels: Dict[str, Any] = {
            "regime_ok": regime_ok,
            "bars_tracked": len(bars),
        }

        # on_book_update() calls _features() internally exactly once.
        # Never call _features() separately first — it is NOT pure:
        # it mutates _mids, _z_mlofi, _z_micro, _z_oi, _z_sector,
        # _basis_hist, and _prev_oi, so calling it twice distorts the
        # rolling statistics.
        try:
            signal = strategy.on_book_update(
                book_snapshot
            )
        except Exception as exc:
            return SingleStrategyPrediction(
                status="ERROR",
                reason=(
                    "SSF-L5-SRM book evaluation "
                    f"failed: {type(exc).__name__}: {exc}"
                ),
                levels=levels,
            )

        # Read features written by on_book_update() to avoid the
        # double-evaluation bug while still exposing feature values.
        features = getattr(
            strategy,
            "last_features",
            {},
        ) or {}

        if features:
            for key, value in features.items():
                number = _finite_number(
                    value
                )
                if number is not None:
                    levels[key] = round(
                        number,
                        3,
                    )

        if signal is not None:
            if signal.action == SignalAction.BUY:
                return self._prediction_from_signal(
                    status="SSF_LONG",
                    signal=signal,
                    ltp=ltp,
                    default_reason=(
                        "SSF Level-5 microstructure "
                        "generated a validated LONG signal."
                    ),
                    levels=levels,
                )

            if signal.action == SignalAction.SELL:
                return self._prediction_from_signal(
                    status="SSF_SHORT",
                    signal=signal,
                    ltp=ltp,
                    default_reason=(
                        "SSF Level-5 microstructure "
                        "generated a validated SHORT signal."
                    ),
                    levels=levels,
                )

        if not regime_ok:
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason=(
                    "SSF regime filter is inactive."
                ),
                levels=levels,
            )

        blocked_reason = None

        if features:
            try:
                blocked_reason = (
                    strategy._blocked(
                        book_snapshot,
                        features,
                    )
                )
            except Exception as exc:
                return SingleStrategyPrediction(
                    status="ERROR",
                    reason=(
                        "SSF-L5-SRM block evaluation "
                        f"failed: {type(exc).__name__}: {exc}"
                    ),
                    levels=levels,
                )

        if blocked_reason:
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason=(
                    "SSF conditions not met "
                    f"({blocked_reason})."
                ),
                levels=levels,
            )

        score = (
            _finite_number(
                features.get("score")
            )
            if features
            else 0.0
        )

        score_text = (
            f"{score:.2f}"
            if score is not None
            else "0.00"
        )

        return SingleStrategyPrediction(
            status="WAITING",
            reason=(
                "Awaiting SSF trigger threshold "
                f"(composite score={score_text})."
            ),
            levels=levels,
        )

    # ================================================================
    # AOU-OSS
    # ================================================================

    def _evaluate_aou_oss(
        self,
        inst: InstrumentConfig,
        df_15m: pd.DataFrame,
        ltp: float,
        book_snapshot: Optional[Any] = None,
    ) -> SingleStrategyPrediction:
        """
        Evaluate AOU-OSS (Analytic Ornstein-Uhlenbeck Optimal Stopping System).
        Completely independent strategy #7.
        """
        if df_15m.empty:
            return SingleStrategyPrediction(
                status="UNAVAILABLE",
                reason="No 15m candle data available.",
                levels={},
                metrics={},
            )

        _, days = self._prepare_data(df_15m)

        if not days:
            return SingleStrategyPrediction(
                status="UNAVAILABLE",
                reason=(
                    "AOU-OSS cannot evaluate: "
                    "no valid trading-session data found."
                ),
                levels={},
                metrics={},
            )

        total_bars = sum(len(d[1]) for d in days)
        if total_bars < 152:
            return SingleStrategyPrediction(
                status="UNAVAILABLE",
                reason="Insufficient 15-minute history for AOU-OSS calibration.",
                levels={},
                metrics={},
            )

        lookback_df = (
            pd.concat([day_df for _, day_df in days[:-1]], ignore_index=True)
            if len(days) > 1
            else pd.DataFrame()
        )

        latest_date, today_df = days[-1]

        strategy = AouOssStrategy(
            inst,
            self.settings.strategy,
        )

        if not lookback_df.empty:
            strategy.seed_context(lookback_df)

        strategy.reset_session(latest_date)

        # Optional live Level-2 market context
        if book_snapshot is not None:
            best_bid = None
            best_ask = None
            bids = getattr(book_snapshot, "bids", None)
            asks = getattr(book_snapshot, "asks", None)
            if bids and len(bids) > 0 and len(bids[0]) > 0:
                best_bid = float(bids[0][0])
            if asks and len(asks) > 0 and len(asks[0]) > 0:
                best_ask = float(asks[0][0])
            strategy.set_market_context(
                best_bid=best_bid,
                best_ask=best_ask,
            )

        current_signal = None
        for _, row in today_df.iterrows():
            candle = self._candle_dict(row)
            close_p = _positive_number(row.get("close")) or ltp
            sig = strategy.on_candle(candle, vwap=float(close_p))
            if sig is not None:
                current_signal = sig

        st = strategy.get_state()
        levels: Dict[str, Any] = {
            "rolling_vwap": _finite_number(st.get("rolling_vwap")),
            "spread": _finite_number(st.get("spread")),
            "equilibrium": _finite_number(st.get("equilibrium")),
            "half_life_minutes": _finite_number(st.get("half_life_minutes")),
            "volatility_ratio": _finite_number(st.get("volatility_ratio")),
            "entry_boundary_long": _finite_number(st.get("entry_boundary_long")),
            "entry_boundary_short": _finite_number(st.get("entry_boundary_short")),
            "stop_boundary_long": _finite_number(st.get("stop_boundary_long")),
            "stop_boundary_short": _finite_number(st.get("stop_boundary_short")),
            "l2_spread_bps": _finite_number(st.get("l2_spread_bps")),
        }
        levels = {
            k: round(v, 4) if isinstance(v, float) else v
            for k, v in levels.items()
            if v is not None
        }

        if current_signal is not None:
            if current_signal.action == SignalAction.BUY:
                return self._prediction_from_signal(
                    status="AOU_LONG",
                    signal=current_signal,
                    ltp=ltp,
                    default_reason=(
                        current_signal.reason
                        or "AOU-OSS optimal stopping generated a validated LONG signal."
                    ),
                    levels=levels,
                )

            if current_signal.action == SignalAction.SELL:
                return self._prediction_from_signal(
                    status="AOU_SHORT",
                    signal=current_signal,
                    ltp=ltp,
                    default_reason=(
                        current_signal.reason
                        or "AOU-OSS optimal stopping generated a validated SHORT signal."
                    ),
                    levels=levels,
                )

        if not st.get("ready"):
            vol_passed = st.get("volatility_passed", False)
            hl = st.get("half_life_minutes")
            if hl is not None and (hl < 15.0 or hl > 60.0):
                reason = f"AOU half-life ({hl:.1f}m) outside 15-60m mean-reverting gate."
            elif not vol_passed and st.get("volatility_ratio") is not None:
                reason = f"AOU volatility ratio ({st.get('volatility_ratio'):.2f}) exceeds threshold."
            elif not st.get("l2_passed", True):
                reason = "AOU Level-2 bid/ask spread exceeds threshold."
            else:
                reason = "AOU-OSS calibration not ready or gates not satisfied."
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason=reason,
                levels=levels,
            )

        spread_val = st.get("spread")
        spread_text = f"{spread_val:.4f}" if spread_val is not None else "0.00"
        return SingleStrategyPrediction(
            status="WAITING",
            reason=f"Awaiting AOU-OSS boundary trigger (spread={spread_text}).",
            levels=levels,
        )

    # ================================================================
    # SIGNAL EXPIRATION
    # ================================================================

    @staticmethod
    def _invalidate_price_breached_signal(
        prediction: SingleStrategyPrediction,
        current_ltp: float,
    ) -> SingleStrategyPrediction:
        """
        Reject any directional signal whose current real LTP has already
        crossed its stop or target.
        """
        if (
            prediction.direction is None
            or current_ltp is None
            or current_ltp <= 0
        ):
            return prediction

        stop = _positive_number(
            prediction.stop_loss
        )
        target = _positive_number(
            prediction.target
        )

        if (
            stop is None
            or target is None
        ):
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason=(
                    "Directional signal has "
                    "invalid stop/target levels."
                ),
                levels=prediction.levels,
                metrics=prediction.metrics,
            )

        if prediction.direction == "LONG":
            if current_ltp <= stop:
                return SingleStrategyPrediction(
                    status="NO_TRADE",
                    reason=(
                        "Signal expired: live LTP "
                        "already crossed the stop."
                    ),
                    levels=prediction.levels,
                    metrics=prediction.metrics,
                )

            if current_ltp >= target:
                return SingleStrategyPrediction(
                    status="NO_TRADE",
                    reason=(
                        "Signal expired: live LTP "
                        "already crossed the target."
                    ),
                    levels=prediction.levels,
                    metrics=prediction.metrics,
                )

        elif prediction.direction == "SHORT":
            if current_ltp >= stop:
                return SingleStrategyPrediction(
                    status="NO_TRADE",
                    reason=(
                        "Signal expired: live LTP "
                        "already crossed the stop."
                    ),
                    levels=prediction.levels,
                    metrics=prediction.metrics,
                )

            if current_ltp <= target:
                return SingleStrategyPrediction(
                    status="NO_TRADE",
                    reason=(
                        "Signal expired: live LTP "
                        "already crossed the target."
                    ),
                    levels=prediction.levels,
                    metrics=prediction.metrics,
                )

        return prediction

    # ================================================================
    # CONSENSUS
    # ================================================================

    @classmethod
    def calculate_consensus(
        cls,
        predictions: Dict[
            str,
            SingleStrategyPrediction,
        ],
    ) -> Dict[str, Any]:
        """
        Transparent equal-vote consensus.

        Status semantics:
        - UNAVAILABLE = required market/history/context data was missing.
        - ERROR = strategy evaluation failed.
        - WAITING = strategy has enough valid data but is waiting for
          its defined entry conditions/time window.
        - NO_TRADE = strategy evaluated successfully and found no valid setup.
        - Directional statuses = validated LONG/SHORT strategy results.

        Only UNAVAILABLE and ERROR are excluded from the evaluable count.
        NO_TRADE and WAITING are valid strategy evaluations without a vote.
        """
        live_predictions = {
            name: prediction
            for name, prediction
            in predictions.items()
            if name in cls.LIVE_CONSENSUS_STRATEGIES
        }

        excluded_strategies = sorted(
            name
            for name in predictions
            if name
            not in cls.LIVE_CONSENSUS_STRATEGIES
        )

        total_live = len(
            live_predictions
        )

        evaluable_count = sum(
            1
            for prediction
            in live_predictions.values()
            if prediction.status
            not in {
                "UNAVAILABLE",
                "ERROR",
            }
        )

        long_count = sum(
            1
            for prediction
            in live_predictions.values()
            if prediction.direction == "LONG"
            and prediction.status
            not in {
                "UNAVAILABLE",
                "ERROR",
            }
        )

        short_count = sum(
            1
            for prediction
            in live_predictions.values()
            if prediction.direction == "SHORT"
            and prediction.status
            not in {
                "UNAVAILABLE",
                "ERROR",
            }
        )

        directional_count = (
            long_count
            + short_count
        )

        if evaluable_count == 0:
            return {
                "direction": "NEUTRAL",
                "agreeing_strategies": 0,
                "total_strategies": total_live,
                "evaluable_strategies": 0,
                "directional_strategies": 0,
                "consensus_agreement_pct": None,
                "label": "UNAVAILABLE",
                "consensus_strategies": list(
                    cls.LIVE_CONSENSUS_STRATEGIES
                ),
                "excluded_strategies": excluded_strategies,
            }

        if directional_count == 0:
            return {
                "direction": "NEUTRAL",
                "agreeing_strategies": 0,
                "total_strategies": total_live,
                "evaluable_strategies": evaluable_count,
                "directional_strategies": 0,
                "consensus_agreement_pct": None,
                "label": "NEUTRAL",
                "consensus_strategies": list(
                    cls.LIVE_CONSENSUS_STRATEGIES
                ),
                "excluded_strategies": excluded_strategies,
            }

        if long_count > 0 and short_count > 0:
            direction = "DIVERGENT"
            agreeing = max(
                long_count,
                short_count,
            )
            label = (
                f"DIVERGENT "
                f"({long_count}L / "
                f"{short_count}S)"
            )

        elif long_count > 0:
            direction = "LONG"
            agreeing = long_count

            if long_count >= 3:
                strength = "STRONG"
            elif long_count >= 2:
                strength = "MODERATE"
            else:
                strength = "WEAK"

            label = (
                f"{strength} LONG "
                f"({long_count}/{evaluable_count})"
            )

        else:
            direction = "SHORT"
            agreeing = short_count

            if short_count >= 3:
                strength = "STRONG"
            elif short_count >= 2:
                strength = "MODERATE"
            else:
                strength = "WEAK"

            label = (
                f"{strength} SHORT "
                f"({short_count}/{evaluable_count})"
            )

        agreement_pct = round(
            (
                agreeing
                / total_live
            )
            * 100.0,
            1,
        ) if total_live > 0 else None

        return {
            "direction": direction,
            "agreeing_strategies": agreeing,
            "total_strategies": total_live,
            "evaluable_strategies": evaluable_count,
            "directional_strategies": directional_count,
            "consensus_agreement_pct": agreement_pct,
            "label": label,
            "consensus_strategies": list(
                cls.LIVE_CONSENSUS_STRATEGIES
            ),
            "excluded_strategies": excluded_strategies,
        }

    # ================================================================
    # INSIGHTS
    # ================================================================

    @staticmethod
    def extract_key_insights(
        candidates: List[CandidatePrediction],
    ) -> Dict[str, Any]:
        """
        Return only directional consensus insights.

        Neutral/unavailable candidates are never presented as top long/short
        or strongest directional consensus.
        """
        if not candidates:
            return {
                "top_long": None,
                "top_short": None,
                "strongest_consensus": None,
                "divergent_signals": [],
            }

        long_candidates = [
            candidate
            for candidate in candidates
            if candidate.consensus.get(
                "direction"
            ) == "LONG"
        ]

        short_candidates = [
            candidate
            for candidate in candidates
            if candidate.consensus.get(
                "direction"
            ) == "SHORT"
        ]

        directional_candidates = [
            candidate
            for candidate in candidates
            if candidate.consensus.get(
                "direction"
            )
            in {"LONG", "SHORT"}
        ]

        def consensus_key(
            candidate: CandidatePrediction,
        ) -> Tuple[int, float]:
            return (
                int(
                    candidate.consensus.get(
                        "agreeing_strategies",
                        0,
                    )
                ),
                float(
                    candidate.momentum_score
                ),
            )

        top_long = max(
            long_candidates,
            key=consensus_key,
            default=None,
        )

        top_short = max(
            short_candidates,
            key=consensus_key,
            default=None,
        )

        strongest = max(
            directional_candidates,
            key=consensus_key,
            default=None,
        )

        divergent = [
            candidate.to_dict()
            for candidate in candidates
            if candidate.consensus.get(
                "direction"
            ) == "DIVERGENT"
        ]

        return {
            "top_long": (
                top_long.to_dict()
                if top_long is not None
                else None
            ),
            "top_short": (
                top_short.to_dict()
                if top_short is not None
                else None
            ),
            "strongest_consensus": (
                strongest.to_dict()
                if strongest is not None
                else None
            ),
            "divergent_signals": divergent,
        }


prediction_service = PredictionService()
