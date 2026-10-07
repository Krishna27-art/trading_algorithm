"""
Unified live strategy prediction service.

Signal/decision-support layer only.

Live safety rules enforced here:
- A live prediction requires a real current LTP supplied by the live feed.
- Completed candles are never used as a substitute for current LTP.
- Missing required OHLCV/VWAP data fails closed.
- Directional signals are validated before publication.
- Price-breached directional signals are invalidated before publication.
- SSF never reconstructs a fresh signal from stale internal direction state.
- CRSD never substitutes a historical hedge close for a live hedge price.
- Consensus percentage is calculated from evaluable strategies only.
- No broker order placement, modification, cancellation, paper trading,
  simulation, random data, or hardcoded live prices/tokens is performed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
import math
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from backend.config.settings import AppSettings, InstrumentConfig, settings
from backend.config.universe import create_instrument_config_for_equity
from backend.data.time_utils import MarketCalendar
from backend.data.time_utils import now_ist_naive
from backend.indicators.vwap import calculate_session_vwap
from backend.monitoring.logger import logger
from backend.strategy.apex_engine import ApexStrategy
from backend.strategy.base_strategy import SignalAction, StrategySignal
from backend.strategy.cpr_strategy import CPRRegimeBreakoutStrategy, Regime
from backend.strategy.dual_ema_strategy import BufferedDualEMAStrategy
from backend.strategy.aou_oss_strategy import AouOssStrategy
from backend.strategy.crsd_strategy import CRSDStrategy
from backend.strategy.orb_strategy import IntradayORBStrategy
from backend.strategy.sector_impulse_strategy import SectorImpulseStrategy
from backend.strategy.ssf_l5_srm_strategy import SsfL5SrmStrategy


STRATEGY_KEYS = (
    "orb",
    "cpr",
    "dual_ema",
    "apex",
    "sector_impulse",
    "ssf_l5_srm",
    "aou_oss",
    "crsd",
)

# CRSD is a pair/relative-value strategy and is intentionally kept outside the
# single-name directional consensus. It is still evaluated independently and
# is returned to the frontend as its own strategy result.
LIVE_CONSENSUS_STRATEGIES = (
    "orb",
    "cpr",
    "dual_ema",
    "apex",
    "sector_impulse",
    "ssf_l5_srm",
    "aou_oss",
)

REQUIRED_CANDLE_COLUMNS = {
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
}

IST = "Asia/Kolkata"
SESSION_OPEN = dt_time(9, 15)
SESSION_CLOSE = dt_time(15, 30)
DEFAULT_TIMEFRAME_MINUTES = 15


# -----------------------------------------------------------------------------
# Scalar/timestamp helpers
# -----------------------------------------------------------------------------

def _finite_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_number(value: Any) -> Optional[float]:
    number = _finite_number(value)
    if number is None or number <= 0:
        return None
    return number


def _normalize_ist_naive(value: Any) -> Optional[datetime]:
    """Normalize aware timestamps to IST and return a naive IST datetime."""
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
    """Recursively convert numpy/pandas scalars to JSON-safe values."""
    if value is None:
        return None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe_scalar(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_scalar(v) for v in value]
    return value


# -----------------------------------------------------------------------------
# Public DTOs
# -----------------------------------------------------------------------------

@dataclass
class SingleStrategyPrediction:
    status: str
    direction: Optional[str] = None
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    reason: str = ""
    levels: Optional[Dict[str, Any]] = None
    metrics: Optional[Dict[str, Any]] = None
    hedge_symbol: Optional[str] = None
    hedge_action: Optional[str] = None
    hedge_entry: Optional[float] = None
    hedge_legs: Optional[Dict[str, float]] = None
    strategy: Optional[str] = None
    symbol: Optional[str] = None
    pair_prices: Optional[Dict[str, float]] = None
    hedge_notional_weights: Optional[Dict[str, float]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"status": self.status}

        if self.strategy is not None:
            payload["strategy"] = self.strategy
        if self.symbol is not None:
            payload["symbol"] = self.symbol
        if self.direction is not None:
            payload["direction"] = self.direction
        if self.entry is not None:
            payload["entry"] = round(float(self.entry), 2)
        if self.stop_loss is not None:
            payload["stop_loss"] = round(float(self.stop_loss), 2)
        if self.target is not None:
            payload["target"] = round(float(self.target), 2)
        if self.hedge_symbol is not None:
            payload["hedge_symbol"] = self.hedge_symbol
        if self.hedge_action is not None:
            payload["hedge_action"] = self.hedge_action
        if self.hedge_entry is not None:
            payload["hedge_entry"] = round(float(self.hedge_entry), 2)
        if self.hedge_legs is not None:
            payload["hedge_legs"] = {
                str(k): round(float(v), 4) for k, v in self.hedge_legs.items()
            }
        if self.hedge_notional_weights is not None:
            payload["hedge_notional_weights"] = {
                str(k): round(float(v), 4)
                for k, v in self.hedge_notional_weights.items()
            }
        if self.pair_prices is not None:
            payload["pair_prices"] = {
                str(k): round(float(v), 2)
                for k, v in self.pair_prices.items()
                if _finite_number(v) is not None and float(v) > 0
            }
        if self.reason:
            payload["reason"] = str(self.reason)

        payload["levels"] = _safe_scalar(self.levels or {})
        payload["metrics"] = _safe_scalar(self.metrics or {})
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
            key: value.to_dict()
            if isinstance(value, SingleStrategyPrediction)
            else _safe_scalar(value)
            for key, value in self.predictions.items()
        }
        return {
            "rank": int(self.rank),
            "symbol": self.symbol,
            "ltp": round(float(self.ltp), 2),
            "momentum_score": round(float(self.momentum_score), 1),
            "universe_bias": self.universe_bias,
            "predictions": predictions,
            "strategies": predictions,
            "consensus": _safe_scalar(self.consensus),
        }


@dataclass
class _AouRuntime:
    strategy: AouOssStrategy
    session_date: date
    last_candle_open: Optional[datetime] = None
    last_signal: Optional[StrategySignal] = None
    lock: Any = field(default_factory=Lock)


class PredictionService:
    """Single source of truth for live strategy evaluation and consensus."""

    LIVE_CONSENSUS_STRATEGIES = LIVE_CONSENSUS_STRATEGIES

    def __init__(self, app_settings: AppSettings = settings):
        self.settings = app_settings
        self.cache_dir = self.settings.base_dir / "backend" / "data" / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._aou_runtimes: Dict[str, _AouRuntime] = {}
        self._aou_runtimes_lock = Lock()

    # ======================================================================
    # INPUT VALIDATION / PREPARATION
    # ======================================================================

    @staticmethod
    def _latest_completed_15m_start(now_ist: datetime) -> Optional[datetime]:
        now = _normalize_ist_naive(now_ist)
        if now is None:
            return None

        session_start = datetime.combine(now.date(), SESSION_OPEN)
        session_close = datetime.combine(now.date(), SESSION_CLOSE)
        first_completed = session_start + timedelta(minutes=DEFAULT_TIMEFRAME_MINUTES)

        if now < first_completed:
            return None

        effective_now = min(now, session_close)
        elapsed_minutes = (effective_now - session_start).total_seconds() / 60.0
        completed_bars = int(elapsed_minutes // DEFAULT_TIMEFRAME_MINUTES)
        if completed_bars <= 0:
            return None

        return session_start + timedelta(
            minutes=(completed_bars - 1) * DEFAULT_TIMEFRAME_MINUTES
        )

    @staticmethod
    def _validate_candle_dataframe(data: pd.DataFrame) -> bool:
        """Validate a live candle dataframe."""
        if data is None or data.empty:
            return False
        if not REQUIRED_CANDLE_COLUMNS.issubset(data.columns):
            return False

        for column in ("open", "high", "low", "close", "volume"):
            data[column] = pd.to_numeric(data[column], errors="coerce")

        if "vwap" in data.columns:
            data["vwap"] = pd.to_numeric(data["vwap"], errors="coerce")

        valid = (
            data["datetime"].notna()
            & data["open"].notna()
            & data["high"].notna()
            & data["low"].notna()
            & data["close"].notna()
            & data["volume"].notna()
            & np.isfinite(data["open"].to_numpy())
            & np.isfinite(data["high"].to_numpy())
            & np.isfinite(data["low"].to_numpy())
            & np.isfinite(data["close"].to_numpy())
            & np.isfinite(data["volume"].to_numpy())
            & (data["open"] > 0)
            & (data["high"] > 0)
            & (data["low"] > 0)
            & (data["close"] > 0)
            & (data["volume"] >= 0)
            & (data["high"] >= data["low"])
            & (data["high"] >= data["open"])
            & (data["high"] >= data["close"])
            & (data["low"] <= data["open"])
            & (data["low"] <= data["close"])
        )
        return bool(valid.all())

    def _prepare_live_candles(self, df_15m: pd.DataFrame) -> pd.DataFrame:
        """
        Keep only completed, valid real candles.

        Historical sessions are retained because several strategies require
        previous-session warm-up. Today's forming candle is excluded.
        """
        if not isinstance(df_15m, pd.DataFrame) or df_15m.empty:
            return pd.DataFrame()

        data = df_15m.copy()
        if not REQUIRED_CANDLE_COLUMNS.issubset(data.columns):
            return pd.DataFrame()

        data["datetime"] = data["datetime"].map(_normalize_ist_naive)
        data = data.dropna(subset=["datetime"]).copy()
        if data.empty or not self._validate_candle_dataframe(data):
            return pd.DataFrame()

        data = data.sort_values("datetime").drop_duplicates(
            subset=["datetime"], keep="last"
        ).reset_index(drop=True)

        now_ist = now_ist_naive()
        data = data[data["datetime"] <= now_ist].copy()
        if data.empty:
            return pd.DataFrame()

        latest_completed = self._latest_completed_15m_start(now_ist)
        today = now_ist.date()
        historical = data[data["datetime"].dt.date < today]
        today_completed = (
            data[
                (data["datetime"].dt.date == today)
                & (latest_completed is not None)
                & (data["datetime"] <= latest_completed)
            ]
            if latest_completed is not None
            else data.iloc[0:0]
        )

        result = pd.concat([historical, today_completed], ignore_index=True)
        return result.sort_values("datetime").reset_index(drop=True)

    def _prepare_data(
        self,
        df_15m: pd.DataFrame,
    ) -> Tuple[pd.DataFrame, List[Tuple[date, pd.DataFrame]]]:
        """
        Prepare data for strategies.

        VWAP is an upstream-required field in live evaluation. It is NOT
        silently reconstructed in this service when missing.
        """
        if not isinstance(df_15m, pd.DataFrame) or df_15m.empty:
            return pd.DataFrame(), []

        data = df_15m.copy()
        if "datetime" not in data.columns and isinstance(data.index, pd.DatetimeIndex):
            data["datetime"] = data.index
        if not REQUIRED_CANDLE_COLUMNS.issubset(data.columns):
            return pd.DataFrame(), []

        data["datetime"] = data["datetime"].map(_normalize_ist_naive)
        data = data.dropna(subset=["datetime"]).copy()
        if data.empty:
            return pd.DataFrame(), []

        if not self._validate_candle_dataframe(data):
            return pd.DataFrame(), []

        data = data.sort_values("datetime")
        data = data.drop_duplicates(subset=["datetime"], keep="last")
        data["date"] = data["datetime"].dt.date
        data = data.reset_index(drop=True)

        if "vwap" not in data.columns:
            data["vwap"] = calculate_session_vwap(data).to_numpy()
        else:
            supplied_vwap = pd.to_numeric(data["vwap"], errors="coerce")
            if supplied_vwap.isna().any() or (supplied_vwap <= 0).any():
                derived = calculate_session_vwap(data)
                use_derived = supplied_vwap.isna() | (supplied_vwap <= 0)
                data["vwap"] = supplied_vwap.where(~use_derived, derived)

        days = list(data.groupby("date", sort=True))
        return data, days

    @staticmethod
    def _resolve_ltp(current_ltp: Optional[float]) -> Optional[float]:
        """
        Resolve ONLY a real current live LTP supplied by the caller.

        A completed candle close is intentionally NOT a fallback.
        """
        return _positive_number(current_ltp)

    @staticmethod
    def _candle_dict(row: pd.Series) -> Dict[str, Any]:
        vol = row.get("volume")
        vol_float = float(vol) if pd.notna(vol) else None
        return {
            "datetime": row["datetime"],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": vol_float,
        }

    # ======================================================================
    # SIGNAL VALIDATION
    # ======================================================================

    @staticmethod
    def _validate_directional_signal(
        signal: Optional[StrategySignal],
        ltp: float,
    ) -> Optional[str]:
        if signal is None:
            return "NO_SIGNAL"

        if signal.action not in {SignalAction.BUY, SignalAction.SELL}:
            return "NON_DIRECTIONAL"

        entry = _positive_number(getattr(signal, "price", None))
        stop = _positive_number(getattr(signal, "stop_loss", None))
        target = _positive_number(getattr(signal, "target", None))
        if entry is None or stop is None or target is None:
            return "INVALID_SIGNAL_LEVELS"

        direction = "LONG" if signal.action == SignalAction.BUY else "SHORT"
        if direction == "LONG":
            if not stop < entry < target:
                return "INVALID_LONG_LEVEL_ORDER"
            if ltp <= stop:
                return "EXPIRED_LONG_STOP"
            if ltp >= target:
                return "EXPIRED_LONG_TARGET"
        else:
            if not target < entry < stop:
                return "INVALID_SHORT_LEVEL_ORDER"
            if ltp >= stop:
                return "EXPIRED_SHORT_STOP"
            if ltp <= target:
                return "EXPIRED_SHORT_TARGET"

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
        validation_error = PredictionService._validate_directional_signal(signal, ltp)
        if validation_error is not None:
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason=f"Signal rejected: {validation_error}.",
                levels=levels or {},
                metrics=metrics or {},
            )

        direction = "LONG" if signal.action == SignalAction.BUY else "SHORT"
        valid_risk, risk_reason = PredictionService._validate_signal_risk(
            direction=direction,
            entry=float(signal.price),
            stop_loss=float(signal.stop_loss),
            target=float(signal.target),
        )
        if not valid_risk:
            return SingleStrategyPrediction(
                status="NO_TRADE",
                direction="NEUTRAL",
                reason=f"Risk validation rejected signal: {risk_reason}",
                levels=levels or {},
                metrics=metrics or {},
            )

        hedge_action_str = (
            signal.hedge_action.value
            if isinstance(signal.hedge_action, SignalAction)
            else (str(signal.hedge_action) if signal.hedge_action else None)
        )

        return SingleStrategyPrediction(
            status=status,
            direction=direction,
            entry=float(signal.price),
            stop_loss=float(signal.stop_loss),
            target=float(signal.target),
            reason=signal.reason or default_reason,
            levels=levels or {},
            metrics=metrics or {},
            hedge_symbol=signal.hedge_symbol,
            hedge_action=hedge_action_str,
            hedge_entry=(
                float(signal.hedge_price)
                if _positive_number(signal.hedge_price) is not None
                else None
            ),
        )

    @staticmethod
    def _prediction_from_crsd_signal(
        status: str,
        signal: StrategySignal,
        default_reason: str,
        levels: Optional[Dict[str, Any]] = None,
        metrics: Optional[Dict[str, Any]] = None,
        min_hedge_legs: int = 1,
    ) -> SingleStrategyPrediction:
        target_sym = str(signal.symbol or "").strip().upper()
        if not target_sym:
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason="CRSD signal rejected: primary target symbol is missing.",
                levels=levels or {}, metrics=metrics or {}, strategy="crsd",
            )

        if signal.action not in (SignalAction.BUY, SignalAction.SELL):
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason=f"CRSD signal rejected: invalid primary action '{signal.action}'.",
                levels=levels or {}, metrics=metrics or {}, strategy="crsd", symbol=target_sym,
            )

        price = _positive_number(signal.price)
        if price is None:
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason=f"CRSD signal rejected: invalid primary price '{signal.price}'.",
                levels=levels or {}, metrics=metrics or {}, strategy="crsd", symbol=target_sym,
            )

        if not isinstance(signal.timestamp, datetime):
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason=f"CRSD signal rejected: invalid timestamp '{signal.timestamp}'.",
                levels=levels or {}, metrics=metrics or {}, strategy="crsd", symbol=target_sym,
            )

        hedge_legs = signal.hedge_legs
        if not isinstance(hedge_legs, dict) or len(hedge_legs) < min_hedge_legs:
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason="CRSD signal rejected: hedge basket is missing or insufficient.",
                levels=levels or {}, metrics=metrics or {}, strategy="crsd", symbol=target_sym,
            )

        validated_basket: Dict[str, float] = {}
        for h_sym, h_weight in hedge_legs.items():
            clean_h_sym = str(h_sym or "").strip().upper()
            if not clean_h_sym or clean_h_sym == target_sym:
                return SingleStrategyPrediction(
                    status="NO_TRADE",
                    reason="CRSD signal rejected: invalid hedge symbol in basket.",
                    levels=levels or {}, metrics=metrics or {}, strategy="crsd", symbol=target_sym,
                )
            try:
                weight = float(h_weight)
            except (TypeError, ValueError):
                return SingleStrategyPrediction(
                    status="NO_TRADE",
                    reason=f"CRSD signal rejected: invalid hedge weight for {clean_h_sym}.",
                    levels=levels or {}, metrics=metrics or {}, strategy="crsd", symbol=target_sym,
                )
            if not math.isfinite(weight) or abs(weight) < 1e-6:
                return SingleStrategyPrediction(
                    status="NO_TRADE",
                    reason=f"CRSD signal rejected: zero/invalid hedge weight for {clean_h_sym}.",
                    levels=levels or {}, metrics=metrics or {}, strategy="crsd", symbol=target_sym,
                )
            validated_basket[clean_h_sym] = weight

        hedge_symbol_str: Optional[str] = None
        hedge_action_str: Optional[str] = None
        hedge_entry_val: Optional[float] = None
        if signal.hedge_symbol:
            clean_primary_hedge = str(signal.hedge_symbol).strip().upper()
            if clean_primary_hedge not in validated_basket:
                return SingleStrategyPrediction(
                    status="NO_TRADE",
                    reason=f"CRSD signal rejected: primary hedge '{clean_primary_hedge}' not in basket.",
                    levels=levels or {}, metrics=metrics or {}, strategy="crsd", symbol=target_sym,
                )
            if signal.hedge_action not in (SignalAction.BUY, SignalAction.SELL):
                return SingleStrategyPrediction(
                    status="NO_TRADE",
                    reason="CRSD signal rejected: invalid hedge action.",
                    levels=levels or {}, metrics=metrics or {}, strategy="crsd", symbol=target_sym,
                )
            if signal.hedge_action == signal.action:
                return SingleStrategyPrediction(
                    status="NO_TRADE",
                    reason="CRSD signal rejected: hedge action must be opposite to primary action.",
                    levels=levels or {}, metrics=metrics or {}, strategy="crsd", symbol=target_sym,
                )
            hedge_entry_val = _positive_number(signal.hedge_price)
            if hedge_entry_val is None:
                return SingleStrategyPrediction(
                    status="NO_TRADE",
                    reason="CRSD signal rejected: live hedge price is invalid.",
                    levels=levels or {}, metrics=metrics or {}, strategy="crsd", symbol=target_sym,
                )
            hedge_symbol_str = clean_primary_hedge
            hedge_action_str = signal.hedge_action.value

        pair_prices = {target_sym: price}
        if hedge_symbol_str is not None and hedge_entry_val is not None:
            pair_prices[hedge_symbol_str] = hedge_entry_val

        return SingleStrategyPrediction(
            status=status,
            direction="LONG" if signal.action == SignalAction.BUY else "SHORT",
            entry=price,
            stop_loss=_positive_number(signal.stop_loss),
            target=_positive_number(signal.target),
            reason=signal.reason or default_reason,
            levels=levels or {},
            metrics=metrics or {},
            hedge_symbol=hedge_symbol_str,
            hedge_action=hedge_action_str,
            hedge_entry=hedge_entry_val,
            hedge_legs=validated_basket,
            strategy="crsd",
            symbol=target_sym,
            pair_prices=pair_prices,
            hedge_notional_weights=validated_basket,
        )

    @staticmethod
    def _validate_signal_risk(
        direction: str,
        entry: Optional[float],
        stop_loss: Optional[float],
        target: Optional[float],
    ) -> Tuple[bool, Optional[str]]:
        if direction not in ("LONG", "SHORT"):
            return True, None
        if entry is None or entry <= 0:
            return False, "Invalid entry price."
        if stop_loss is None or stop_loss <= 0:
            return False, "Invalid stop loss."
        if direction == "LONG":
            if stop_loss >= entry:
                return False, f"Long stop loss ({stop_loss}) must be below entry ({entry})."
            if target is not None and target <= entry:
                return False, f"Long target ({target}) must be above entry ({entry})."
        elif direction == "SHORT":
            if stop_loss <= entry:
                return False, f"Short stop loss ({stop_loss}) must be above entry ({entry})."
            if target is not None and target >= entry:
                return False, f"Short target ({target}) must be below entry ({entry})."
        return True, None

    def _get_live_apex_context(
        self,
        symbol: str,
        book_snapshot: Optional[Any] = None,
        kite_client: Optional[Any] = None,
        live_ltp_by_symbol: Optional[Dict[str, Any]] = None,
        peer_context: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        india_vix = None
        if live_ltp_by_symbol:
            raw_vix = live_ltp_by_symbol.get("INDIA VIX") or live_ltp_by_symbol.get("INDIAVIX")
            if isinstance(raw_vix, tuple) and len(raw_vix) == 2:
                india_vix = _positive_number(raw_vix[0])
            elif raw_vix is not None:
                india_vix = _positive_number(raw_vix)

        order_imbalance = None
        if book_snapshot is not None:
            if isinstance(book_snapshot, dict):
                buy_depth = book_snapshot.get("buy", [])
                sell_depth = book_snapshot.get("sell", [])
                total_buy_qty = sum(item.get("quantity", 0) for item in buy_depth if isinstance(item, dict))
                total_sell_qty = sum(item.get("quantity", 0) for item in sell_depth if isinstance(item, dict))
                if (total_buy_qty + total_sell_qty) > 0:
                    order_imbalance = (total_buy_qty - total_sell_qty) / float(total_buy_qty + total_sell_qty)
            elif hasattr(book_snapshot, "bids") and hasattr(book_snapshot, "asks"):
                buy_depth = getattr(book_snapshot, "bids", []) or []
                sell_depth = getattr(book_snapshot, "asks", []) or []
                total_buy_qty = sum(
                    int(level[1])
                    for level in buy_depth[:5]
                    if isinstance(level, (list, tuple)) and len(level) >= 2
                )
                total_sell_qty = sum(
                    int(level[1])
                    for level in sell_depth[:5]
                    if isinstance(level, (list, tuple)) and len(level) >= 2
                )
                if (total_buy_qty + total_sell_qty) > 0:
                    order_imbalance = (total_buy_qty - total_sell_qty) / float(total_buy_qty + total_sell_qty)

        sector_rs = None
        market_rs = None
        if peer_context is not None and hasattr(peer_context, "sector_rs"):
            sector_rs = _finite_number(getattr(peer_context, "sector_rs", None))
            market_rs = _finite_number(getattr(peer_context, "market_rs", None))

        if india_vix is None and order_imbalance is None and sector_rs is None and market_rs is None:
            return None

        return {
            "india_vix": india_vix,
            "calendar_blackout": False,
            "gift_nifty_gap": None,
            "sector_rs": sector_rs,
            "market_rs": market_rs,
            "catalyst_score": None,
            "order_imbalance": order_imbalance,
        }

    @staticmethod
    def _error_prediction(strategy_name: str, exc: Exception) -> SingleStrategyPrediction:
        return SingleStrategyPrediction(
            status="ERROR",
            reason=f"{strategy_name} evaluation failed: {type(exc).__name__}: {exc}",
            levels={}, metrics={},
        )

    # ======================================================================
    # PUBLIC EVALUATION
    # ======================================================================

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
        live_ltp_by_symbol: Optional[Dict[str, Any]] = None,
        crsd_context: Optional[Any] = None,
    ) -> Tuple[Dict[str, SingleStrategyPrediction], Dict[str, Any]]:
        clean_symbol = str(symbol).strip().upper()
        resolved_crsd_context = crsd_context
        resolved_peer_context = peer_context
        if resolved_crsd_context is None and resolved_peer_context is not None:
            if hasattr(resolved_peer_context, "market") and not hasattr(resolved_peer_context, "before"):
                resolved_crsd_context = resolved_peer_context
                resolved_peer_context = None

        live_df = self._prepare_live_candles(df_15m)

        if live_df.empty:
            predictions = {
                key: SingleStrategyPrediction(
                    status="UNAVAILABLE",
                    reason="No completed valid 15-minute OHLCV/VWAP candle data is available.",
                    levels={}, metrics={},
                )
                for key in STRATEGY_KEYS
            }
            return predictions, self.calculate_consensus(predictions)

        # CRITICAL: current price must come from the live quote/tick path.
        ltp = self._resolve_ltp(current_ltp)
        if ltp is None:
            predictions = {
                key: SingleStrategyPrediction(
                    status="UNAVAILABLE",
                    reason="No valid real current LTP was supplied by the live market-data path.",
                    levels={}, metrics={},
                )
                for key in STRATEGY_KEYS
            }
            return predictions, self.calculate_consensus(predictions)

        _, days = self._prepare_data(live_df)
        if not days:
            predictions = {
                key: SingleStrategyPrediction(
                    status="UNAVAILABLE",
                    reason="No valid trading-session data remains after live validation.",
                    levels={}, metrics={},
                )
                for key in STRATEGY_KEYS
            }
            return predictions, self.calculate_consensus(predictions)

        today = now_ist_naive().date()
        latest_date, current_session_df = days[-1]
        if latest_date != today:
            predictions = {
                key: SingleStrategyPrediction(
                    status="UNAVAILABLE",
                    reason=(
                        f"Current-session completed candle data is unavailable. "
                        f"Latest available session={latest_date}, today={today}."
                    ),
                    levels={},
                    metrics={},
                )
                for key in STRATEGY_KEYS
            }
            return predictions, self.calculate_consensus(predictions)

        # Use the current session's opening price for session-level calculations.
        session_reference_price = _positive_number(current_session_df["open"].iloc[0])
        if session_reference_price is None:
            predictions = {
                key: SingleStrategyPrediction(
                    status="UNAVAILABLE",
                    reason="Current-session opening price is invalid.",
                    levels={}, metrics={},
                )
                for key in STRATEGY_KEYS
            }
            return predictions, self.calculate_consensus(predictions)

        # Lot size is metadata and must come from the actual instrument master.
        lot_size: Optional[int] = None
        if kite_client is None:
            try:
                from backend.broker.kite_adapter import get_active_kite
                kite_client = get_active_kite()
            except Exception:
                pass

        try:
            from backend.data.instrument_resolver import instrument_resolver
            lot_size = (
                instrument_resolver.resolve_lot_size(
                    clean_symbol,
                    exchange="NSE",
                    instrument_type="EQ",
                    kite_client=kite_client,
                    fallback=None,
                )
            )
        except Exception as exc:
            logger.warning(
                "Live instrument lot-size resolution "
                "failed for %s: %s",
                clean_symbol,
                exc,
            )
            lot_size = None

        if lot_size is None or lot_size <= 0:
            predictions = {
                key: SingleStrategyPrediction(
                    status="UNAVAILABLE",
                    reason=(
                        "Live instrument lot size is unavailable."
                    ),
                    levels={},
                    metrics={},
                )
                for key in STRATEGY_KEYS
            }
            return (
                predictions,
                self.calculate_consensus(predictions),
            )

        try:
            inst = create_instrument_config_for_equity(
                clean_symbol,
                token,
                current_price=float(session_reference_price),
                lot_size=lot_size,
            )
        except Exception as exc:
            predictions = {
                key: SingleStrategyPrediction(
                    status="ERROR",
                    reason=f"Instrument configuration failed: {type(exc).__name__}: {exc}",
                    levels={}, metrics={},
                )
                for key in STRATEGY_KEYS
            }
            return predictions, self.calculate_consensus(predictions)

        predictions: Dict[str, SingleStrategyPrediction] = {}

        evaluators = (
            ("orb", "ORB", lambda: self._evaluate_orb(inst, live_df, ltp)),
            ("cpr", "CPR", lambda: self._evaluate_cpr(inst, live_df, ltp)),
            ("dual_ema", "Dual EMA", lambda: self._evaluate_dual_ema(inst, live_df, ltp)),
            (
                "apex",
                "APEX",
                lambda: self._evaluate_apex(
                    inst,
                    live_df,
                    ltp,
                    book_snapshot=book_snapshot,
                    kite_client=kite_client,
                    live_ltp_by_symbol=live_ltp_by_symbol,
                    peer_context=resolved_peer_context,
                ),
            ),
            (
                "sector_impulse",
                "Sector Impulse",
                lambda: self._evaluate_sector_impulse(
                    inst, live_df, ltp, peer_context=resolved_peer_context, kite_client=kite_client
                ),
            ),
            (
                "ssf_l5_srm",
                "SSF-L5-SRM",
                lambda: self._evaluate_ssf_l5_srm(
                    inst, live_df, ltp, book_snapshot=book_snapshot, ssf_strategy=ssf_strategy
                ),
            ),
            (
                "aou_oss",
                "AOU-OSS",
                lambda: self._evaluate_aou_oss(
                    inst,
                    live_df,
                    ltp,
                    book_snapshot=book_snapshot,
                    allow_historical_session=False,
                ),
            ),
            (
                "crsd",
                "CRSD",
                lambda: self._evaluate_crsd(
                    inst,
                    live_df,
                    ltp,
                    peer_context=resolved_crsd_context,
                    kite_client=kite_client,
                    live_ltp_by_symbol=live_ltp_by_symbol,
                ),
            ),
        )

        for key, display_name, evaluator in evaluators:
            try:
                predictions[key] = evaluator()
            except Exception as exc:
                predictions[key] = self._error_prediction(display_name, exc)

        # Wire signal expiration into the actual publication path.
        predictions = {
            key: self._invalidate_price_breached_signal(prediction, ltp)
            for key, prediction in predictions.items()
        }

        return predictions, self.calculate_consensus(predictions)

    # ======================================================================
    # ORB
    # ======================================================================

    def _evaluate_orb(self, inst: InstrumentConfig, df_15m: pd.DataFrame, ltp: float) -> SingleStrategyPrediction:
        _, days = self._prepare_data(df_15m)
        if not days:
            return SingleStrategyPrediction("UNAVAILABLE", reason="No valid ORB session data.")

        latest_date, today_df = days[-1]
        strategy = IntradayORBStrategy(inst, self.settings.strategy)
        strategy.reset_session(latest_date)
        current_signal: Optional[StrategySignal] = None

        for _, row in today_df.iterrows():
            sig = strategy.on_candle(self._candle_dict(row), float(row["vwap"]))
            if sig is not None:
                if sig.action in (SignalAction.BUY, SignalAction.SELL):
                    current_signal = sig
                elif sig.action in (SignalAction.STOP_LOSS, SignalAction.TARGET, SignalAction.EXIT):
                    current_signal = None

        orb_info: Dict[str, Any] = {}
        if strategy.orb is not None:
            orb_info = {
                "orb_high": round(float(strategy.orb.high), 2),
                "orb_low": round(float(strategy.orb.low), 2),
                "orb_width": round(float(strategy.orb.width), 2),
                "is_valid_volatility": bool(strategy.orb.is_valid_volatility),
            }

        if current_signal is not None and current_signal.action == SignalAction.BUY:
            return self._prediction_from_signal(
                "LONG_BREAKOUT", current_signal, ltp,
                "Price broke above the 30-minute ORB high with VWAP confirmation.", orb_info
            )
        if current_signal is not None and current_signal.action == SignalAction.SELL:
            return self._prediction_from_signal(
                "SHORT_BREAKDOWN", current_signal, ltp,
                "Price broke below the 30-minute ORB low with VWAP confirmation.", orb_info
            )
        if strategy.orb is None:
            return SingleStrategyPrediction(
                "WAITING", reason="Establishing the 30-minute opening range (09:15-09:45 IST).",
                levels=orb_info,
            )
        if not strategy.orb.is_valid_volatility:
            return SingleStrategyPrediction(
                "NO_TRADE", reason="Opening range failed the configured volatility filter.", levels=orb_info
            )
        return SingleStrategyPrediction(
            "NO_TRADE", reason="No valid ORB breakout signal on the latest completed candle.", levels=orb_info
        )

    # ======================================================================
    # CPR
    # ======================================================================

    def _evaluate_cpr(self, inst: InstrumentConfig, df_15m: pd.DataFrame, ltp: float) -> SingleStrategyPrediction:
        _, days = self._prepare_data(df_15m)
        if len(days) < 2:
            return SingleStrategyPrediction(
                "UNAVAILABLE", reason="CPR requires prior-session data.", levels={}, metrics={}
            )

        lookback_df = pd.concat([day_df for _, day_df in days[:-1]], ignore_index=True)
        latest_date, today_df = days[-1]
        strategy = CPRRegimeBreakoutStrategy(inst, self.settings.strategy)
        strategy.seed_context(lookback_df)
        strategy.reset_session(latest_date)
        current_signal = None

        for _, row in today_df.iterrows():
            sig = strategy.on_candle(self._candle_dict(row), float(row["vwap"]))
            if sig is not None:
                if sig.action in (SignalAction.BUY, SignalAction.SELL):
                    current_signal = sig
                elif sig.action in (SignalAction.STOP_LOSS, SignalAction.TARGET, SignalAction.EXIT):
                    current_signal = None

        levels: Dict[str, Any] = {}
        if strategy.pivots:
            levels = {
                "pivot": round(float(strategy.pivots["P"]), 2),
                "bottom_central": round(float(strategy.pivots["BC"]), 2),
                "top_central": round(float(strategy.pivots["TC"]), 2),
                "r1": round(float(strategy.pivots["R1"]), 2),
                "s1": round(float(strategy.pivots["S1"]), 2),
                "cpr_width_pct": round(float(strategy.pivots["width_pct"]), 2),
                "regime": _safe_scalar(strategy.regime),
            }

        if current_signal is not None and current_signal.action == SignalAction.BUY:
            return self._prediction_from_signal("BULLISH_EXPANSION", current_signal, ltp,
                "Valid CPR bullish breakout on the latest completed candle.", levels)
        if current_signal is not None and current_signal.action == SignalAction.SELL:
            return self._prediction_from_signal("BEARISH_EXPANSION", current_signal, ltp,
                "Valid CPR bearish breakdown on the latest completed candle.", levels)

        return SingleStrategyPrediction(
            "NO_TRADE",
            reason=("CPR regime is neutral; no directional setup is active." if strategy.regime == Regime.NEUTRAL
                    else "No valid CPR trigger on the latest completed candle."),
            levels=levels,
        )

    # ======================================================================
    # DUAL EMA
    # ======================================================================

    def _evaluate_dual_ema(self, inst: InstrumentConfig, df_15m: pd.DataFrame, ltp: float) -> SingleStrategyPrediction:
        _, days = self._prepare_data(df_15m)
        if len(days) < 2:
            return SingleStrategyPrediction(
                "UNAVAILABLE",
                reason="Dual-EMA requires prior-session history for SMA200/ATR warm-up.",
                levels={}, metrics={},
            )

        lookback_df = pd.concat([day_df for _, day_df in days[:-1]], ignore_index=True)
        latest_date, today_df = days[-1]
        strategy = BufferedDualEMAStrategy(inst, self.settings.strategy)
        strategy.seed_context(lookback_df)
        strategy.reset_session(latest_date)
        current_signal = None

        for _, row in today_df.iterrows():
            sig = strategy.on_candle(self._candle_dict(row), float(row["vwap"]))
            if sig is not None:
                if sig.action in (SignalAction.BUY, SignalAction.SELL):
                    current_signal = sig
                elif sig.action in (SignalAction.STOP_LOSS, SignalAction.TARGET, SignalAction.EXIT):
                    current_signal = None

        levels: Dict[str, Any] = {}
        current_indicators, _ = strategy.latest_indicators()
        if current_indicators is not None:
            atr = _positive_number(current_indicators.get("atr14"))
            if atr is not None:
                levels = {
                    "ema_fast": round(float(current_indicators["ema9"]), 2),
                    "ema_slow": round(float(current_indicators["ema21"]), 2),
                    "sma_trend": round(float(current_indicators["sma200"]), 2),
                    "atr_14": round(float(current_indicators["atr14"]), 2),
                    "buffer": round(float(strategy.buffer_gamma * atr), 2),
                }

        if current_signal is not None and current_signal.action == SignalAction.BUY:
            return self._prediction_from_signal("TRENDING_LONG", current_signal, ltp,
                "Validated bullish Dual-EMA buffer crossover with SMA200 trend confirmation.", levels)
        if current_signal is not None and current_signal.action == SignalAction.SELL:
            return self._prediction_from_signal("TRENDING_SHORT", current_signal, ltp,
                "Validated bearish Dual-EMA buffer crossover with SMA200 trend confirmation.", levels)
        if current_indicators is None:
            return SingleStrategyPrediction("UNAVAILABLE",
                reason="Insufficient valid history for the required SMA200/ATR14 warm-up.", levels=levels)
        return SingleStrategyPrediction("NO_TRADE",
            reason="No valid Dual-EMA crossover signal on the latest completed candle.", levels=levels)

    # ======================================================================
    # APEX
    # ======================================================================

    def _evaluate_apex(
        self,
        inst: InstrumentConfig,
        df_15m: pd.DataFrame,
        ltp: float,
        book_snapshot: Optional[Any] = None,
        kite_client: Optional[Any] = None,
        live_ltp_by_symbol: Optional[Dict[str, Any]] = None,
        peer_context: Optional[Any] = None,
    ) -> SingleStrategyPrediction:
        _, days = self._prepare_data(df_15m)
        if len(days) < 2:
            return SingleStrategyPrediction(
                "UNAVAILABLE",
                reason="APEX requires prior-session history for ATR/gap context.", levels={}, metrics={}
            )

        latest_date, today_df = days[-1]
        lookback_df = pd.concat([day_df for _, day_df in days[:-1]], ignore_index=True)
        strategy = ApexStrategy(inst, self.settings.strategy)
        strategy.seed_context(lookback_df)
        strategy.reset_session(latest_date)

        apex_context = self._get_live_apex_context(
            symbol=inst.symbol,
            book_snapshot=book_snapshot,
            kite_client=kite_client,
            live_ltp_by_symbol=live_ltp_by_symbol,
            peer_context=peer_context,
        )

        if apex_context is None:
            return SingleStrategyPrediction(
                "UNAVAILABLE",
                reason="APEX required live market context is unavailable.",
                levels={},
                metrics={},
            )

        strategy.set_context(**apex_context)
        current_signal = None

        for _, row in today_df.iterrows():
            sig = strategy.on_candle(self._candle_dict(row), float(row["vwap"]))
            if sig is not None:
                if sig.action in (SignalAction.BUY, SignalAction.SELL):
                    current_signal = sig
                elif sig.action in (SignalAction.STOP_LOSS, SignalAction.TARGET, SignalAction.EXIT):
                    current_signal = None

        analysis = getattr(strategy, "last_analysis", None) or {}
        levels = {
            key: _safe_scalar(value)
            for key, value in analysis.items()
            if key not in {"status", "reason", "direction", "entry", "stop_loss", "target"}
        }

        if current_signal is not None and current_signal.action == SignalAction.BUY:
            return self._prediction_from_signal("APEX_LONG", current_signal, ltp,
                "APEX generated a validated bullish signal.", levels)
        if current_signal is not None and current_signal.action == SignalAction.SELL:
            return self._prediction_from_signal("APEX_SHORT", current_signal, ltp,
                "APEX generated a validated bearish signal.", levels)

        status = str(analysis.get("status", "NO_TRADE"))
        if status in {"LONG", "SHORT", "BUY", "SELL"}:
            status = "NO_TRADE"
        return SingleStrategyPrediction(status=status,
            reason=str(analysis.get("reason", "APEX criteria not met.")), levels=levels)

    # ======================================================================
    # SECTOR IMPULSE
    # ======================================================================

    def _evaluate_sector_impulse(
        self,
        inst: InstrumentConfig,
        df_15m: pd.DataFrame,
        ltp: float,
        peer_context: Optional[Any] = None,
        kite_client: Optional[Any] = None,
    ) -> SingleStrategyPrediction:
        _, days = self._prepare_data(df_15m)
        if not days:
            return SingleStrategyPrediction("UNAVAILABLE", reason="No valid sector-impulse session data.")

        latest_date, today_df = days[-1]
        lookback_df = (
            pd.concat([day_df for _, day_df in days[:-1]], ignore_index=True)
            if len(days) > 1 else pd.DataFrame()
        )
        ctx = peer_context
        if ctx is None or not hasattr(ctx, "before"):
            try:
                from backend.data.sector_peer_manager import SectorPeerManager
                ctx = SectorPeerManager.build_peer_context(
                    symbol=inst.symbol,
                    cache_dir=self.cache_dir,
                    kite_client=kite_client,
                )
            except Exception as exc:
                return SingleStrategyPrediction(
                    "UNAVAILABLE",
                    reason=f"Sector peer context unavailable: {type(exc).__name__}: {exc}",
                    levels={}, metrics={},
                )

        if ctx is None:
            return SingleStrategyPrediction(
                "UNAVAILABLE", reason="Sector leader/index peer context is unavailable.", levels={}, metrics={}
            )

        strategy = SectorImpulseStrategy(inst, self.settings.strategy, ctx=ctx)
        strategy.seed_context(lookback_df)
        strategy.reset_session(latest_date)
        current_signal = None

        for _, row in today_df.iterrows():
            sig = strategy.on_candle(self._candle_dict(row), float(row["vwap"]))
            if sig is not None:
                if sig.action in (SignalAction.BUY, SignalAction.SELL):
                    current_signal = sig
                elif sig.action in (SignalAction.STOP_LOSS, SignalAction.TARGET, SignalAction.EXIT):
                    current_signal = None

        model = getattr(strategy, "model", None)
        levels: Dict[str, Any] = {}
        if model:
            rho = _finite_number(model.get("rho"))
            mkt_sig = _finite_number(model.get("mkt_sig"))
            levels = {
                "lead_lag_k": _safe_scalar(model.get("k")),
                "rho": round(rho, 3) if rho is not None else None,
                "mkt_sig": round(mkt_sig, 4) if mkt_sig is not None else None,
            }

        if current_signal is not None and current_signal.action == SignalAction.BUY:
            return self._prediction_from_signal("IMPULSE_LONG", current_signal, ltp,
                "Sector impulse generated a validated LONG signal.", levels)
        if current_signal is not None and current_signal.action == SignalAction.SELL:
            return self._prediction_from_signal("IMPULSE_SHORT", current_signal, ltp,
                "Sector impulse generated a validated SHORT signal.", levels)

        if model is None:
            reason = str(getattr(strategy, "disabled_reason", None) or "Sector impulse model is not currently eligible.")
            return SingleStrategyPrediction("NO_TRADE", reason=reason, levels=levels)

        rho = _finite_number(model.get("rho"))
        reason = (
            f"Monitoring sector impulse transmission (k={model.get('k')}, rho={rho:.2f})."
            if rho is not None
            else f"Monitoring sector impulse transmission (k={model.get('k')}, rho=N/A)."
        )
        return SingleStrategyPrediction(
            "MONITORING",
            reason=reason,
            levels=levels,
        )

    # ======================================================================
    # CRSD
    # ======================================================================

    def _evaluate_crsd(
        self,
        inst: InstrumentConfig,
        df_15m: pd.DataFrame,
        ltp: float,
        peer_context: Optional[Any] = None,
        kite_client: Optional[Any] = None,
        live_ltp_by_symbol: Optional[Dict[str, float]] = None,
    ) -> SingleStrategyPrediction:
        _, days = self._prepare_data(df_15m)
        if not days:
            return SingleStrategyPrediction("UNAVAILABLE", reason="No valid CRSD session data.")

        try:
            from backend.data.sector_peer_manager import SectorPeerManager
            if SectorPeerManager.get_sector_for_symbol(inst.symbol) is None:
                return SingleStrategyPrediction(
                    "UNAVAILABLE",
                    reason=f"CRSD has no sector classification for {inst.symbol}.",
                    strategy="crsd", symbol=inst.symbol,
                )
        except Exception as exc:
            return SingleStrategyPrediction(
                "UNAVAILABLE", reason=f"CRSD sector classification failed: {exc}",
                strategy="crsd", symbol=inst.symbol,
            )

        latest_date, today_df = days[-1]
        lookback_df = (
            pd.concat([day_df for _, day_df in days[:-1]], ignore_index=True)
            if len(days) > 1 else pd.DataFrame()
        )
        ctx = peer_context

        if ctx is None or not hasattr(ctx, "frames") or not hasattr(ctx, "market"):
            try:
                from backend.strategy.crsd_strategy import build_crsd_context
                ctx = build_crsd_context(
                    symbol=inst.symbol,
                    cache_dir=self.cache_dir,
                    kite_client=kite_client,
                )
            except Exception as exc:
                return SingleStrategyPrediction(
                    "UNAVAILABLE",
                    reason=f"CRSD peer context build failed: {type(exc).__name__}: {exc}",
                    strategy="crsd", symbol=inst.symbol,
                )

        if ctx is None:
            return SingleStrategyPrediction(
                "UNAVAILABLE", reason="CRSD peer/market context is unavailable.",
                strategy="crsd", symbol=inst.symbol,
            )

        strategy = CRSDStrategy(inst, self.settings.strategy, ctx=ctx)
        strategy.seed_context(lookback_df)
        strategy.reset_session(latest_date)
        current_signal = None

        for _, row in today_df.iterrows():
            sig = strategy.on_candle(self._candle_dict(row), float(row["vwap"]))
            if sig is not None:
                if sig.action in (SignalAction.BUY, SignalAction.SELL):
                    current_signal = sig
                elif sig.action in (SignalAction.STOP_LOSS, SignalAction.TARGET, SignalAction.EXIT):
                    current_signal = None

        model = getattr(strategy, "model", None)
        levels: Dict[str, Any] = {}
        if model:
            for key, digits in (("lam", 3), ("entry_z", 2), ("exit_z", 2), ("sigma_d", 6)):
                value = _finite_number(model.get(key))
                if value is not None:
                    levels[key] = round(value, digits)
            syms = model.get("syms", [])
            sel = model.get("sel", [])
            levels["hedge_peers"] = [syms[k] for k in sel if isinstance(k, int) and 0 <= k < len(syms)]

        last_features = getattr(strategy, "last_features", {}) or {}
        metrics: Dict[str, Any] = {}
        for key, digits in (("z", 3), ("z_cs", 3), ("cp_recent", 3), ("stress", 3), ("spread", 6)):
            value = _finite_number(last_features.get(key))
            if value is not None:
                metrics[key] = round(value, digits)
        metrics["liq_ok"] = bool(last_features.get("liq_ok", False))
        risk_scale = _finite_number(getattr(strategy, "risk_scale", None))
        if risk_scale is not None:
            levels["risk_scale"] = risk_scale
            metrics["risk_scale"] = risk_scale

        if current_signal is not None and current_signal.action in (SignalAction.BUY, SignalAction.SELL):
            # CRSD is a pair strategy. A live hedge quote is mandatory.
            hedge_legs = getattr(strategy, "hedge_legs", {}) or {}
            if not hedge_legs:
                return SingleStrategyPrediction(
                    "UNAVAILABLE",
                    reason="CRSD generated a directional candidate without a validated hedge basket.",
                    levels=levels, metrics=metrics, strategy="crsd", symbol=inst.symbol,
                )

            validated_hedge_prices = {}
            now = now_ist_naive()

            for hedge_sym in hedge_legs:
                raw_h = live_ltp_by_symbol.get(hedge_sym) if live_ltp_by_symbol else None
                if not isinstance(raw_h, tuple) or len(raw_h) != 2:
                    if isinstance(raw_h, (int, float)) and raw_h > 0:
                        h_price = float(raw_h)
                        h_ts = now
                    else:
                        return SingleStrategyPrediction(
                            "UNAVAILABLE",
                            reason=f"CRSD live hedge price is unavailable for {hedge_sym}.",
                            levels=levels,
                            metrics=metrics,
                            strategy="crsd",
                            symbol=inst.symbol,
                        )
                else:
                    h_price, h_ts = raw_h
                    h_price = _positive_number(h_price)

                if h_price is None or h_ts is None:
                    return SingleStrategyPrediction(
                        "UNAVAILABLE",
                        reason=f"CRSD live hedge price is unavailable for {hedge_sym}.",
                        levels=levels,
                        metrics=metrics,
                        strategy="crsd",
                        symbol=inst.symbol,
                    )

                if hasattr(h_ts, "tzinfo") and h_ts.tzinfo is not None:
                    h_ts = h_ts.replace(tzinfo=None)
                age = (now - h_ts).total_seconds()

                if age < 0 or age > 120:
                    return SingleStrategyPrediction(
                        "UNAVAILABLE",
                        reason=f"CRSD hedge price for {hedge_sym} is stale (age={age:.1f}s).",
                        levels=levels,
                        metrics=metrics,
                        strategy="crsd",
                        symbol=inst.symbol,
                    )

                validated_hedge_prices[hedge_sym] = h_price

            top_hedge_sym = max(hedge_legs, key=lambda s: abs(hedge_legs[s]))
            hedge_weight = float(hedge_legs[top_hedge_sym])
            current_signal.hedge_symbol = top_hedge_sym
            current_signal.hedge_action = SignalAction.SELL if hedge_weight < 0 else SignalAction.BUY
            current_signal.hedge_price = validated_hedge_prices[top_hedge_sym]

            status = "CRSD_LONG" if current_signal.action == SignalAction.BUY else "CRSD_SHORT"
            return self._prediction_from_crsd_signal(
                status=status,
                signal=current_signal,
                default_reason="CRSD relative-value shock generated a validated signal.",
                levels=levels,
                metrics=metrics,
            )

        if model is None:
            reason = str(getattr(strategy, "disabled_reason", None) or "CRSD model is not currently eligible.")
            return SingleStrategyPrediction("NO_TRADE", reason=reason, levels=levels, metrics=metrics,
                strategy="crsd", symbol=inst.symbol)

        z_val = _finite_number(last_features.get("z"))
        return SingleStrategyPrediction(
            "MONITORING",
            reason=(
                f"Monitoring CRSD residual divergence (Z={z_val:.2f}, entry_Z={model.get('entry_z', 'N/A')})."
                if z_val is not None
                else f"Monitoring CRSD residual divergence (Z=N/A, entry_Z={model.get('entry_z', 'N/A')})."
            ),
            levels=levels, metrics=metrics, strategy="crsd", symbol=inst.symbol,
        )

    # ======================================================================
    # SSF-L5-SRM
    # ======================================================================

    def _evaluate_ssf_l5_srm(
        self,
        inst: InstrumentConfig,
        df_15m: pd.DataFrame,
        ltp: float,
        book_snapshot: Optional[Any] = None,
        ssf_strategy: Optional[Any] = None,
    ) -> SingleStrategyPrediction:
        # The live L5 path requires a real five-level book snapshot. If a
        # persistent strategy object was supplied, it still must have a fresh
        # current book for this evaluation; the strategy state is not a signal.
        bids = getattr(book_snapshot, "bids", None)
        asks = getattr(book_snapshot, "asks", None)
        if book_snapshot is None or not isinstance(bids, (list, tuple)) or not isinstance(asks, (list, tuple)):
            return SingleStrategyPrediction(
                "UNAVAILABLE", reason="SSF-L5-SRM requires a real Level-5 book snapshot.", levels={}, metrics={}
            )
        if len(bids) < 5 or len(asks) < 5:
            return SingleStrategyPrediction(
                "UNAVAILABLE", reason="SSF-L5-SRM requires five valid bid and ask levels.", levels={}, metrics={}
            )

        strategy = ssf_strategy
        if strategy is None:
            strategy = SsfL5SrmStrategy(inst, self.settings.strategy, signal_only=True)

        try:
            signal = strategy.on_book_update(book_snapshot)
        except Exception as exc:
            return SingleStrategyPrediction(
                "ERROR", reason=f"SSF-L5-SRM book evaluation failed: {type(exc).__name__}: {exc}", levels={}
            )

        regime_ok = bool(getattr(strategy, "regime_ok", False))
        bars = getattr(strategy, "_bars", [])
        features = getattr(strategy, "last_features", {}) or {}
        levels: Dict[str, Any] = {
            "regime_ok": regime_ok,
            "bars_tracked": len(bars),
        }
        for key, value in features.items():
            number = _finite_number(value)
            if number is not None:
                levels[key] = round(number, 3)

        # Do not infer a new signal from _last_signal_direction. Only the
        # actual StrategySignal returned by this fresh book evaluation votes.
        if signal is not None and signal.action == SignalAction.BUY:
            return self._prediction_from_signal(
                "SSF_LONG", signal, ltp,
                "SSF Level-5 microstructure generated a validated LONG signal.", levels
            )
        if signal is not None and signal.action == SignalAction.SELL:
            return self._prediction_from_signal(
                "SSF_SHORT", signal, ltp,
                "SSF Level-5 microstructure generated a validated SHORT signal.", levels
            )

        if not regime_ok:
            return SingleStrategyPrediction("NO_TRADE", reason="SSF regime filter is inactive.", levels=levels)

        if features:
            try:
                blocked_reason = strategy._blocked(book_snapshot, features)
            except Exception as exc:
                return SingleStrategyPrediction(
                    "ERROR", reason=f"SSF-L5-SRM block evaluation failed: {type(exc).__name__}: {exc}", levels=levels
                )
            if blocked_reason:
                return SingleStrategyPrediction(
                    "NO_TRADE", reason=f"SSF conditions not met ({blocked_reason}).", levels=levels
                )

        score = _finite_number(features.get("score"))
        score_text = f"{score:.2f}" if score is not None else "N/A"
        return SingleStrategyPrediction(
            "WAITING", reason=f"Awaiting SSF trigger threshold (composite score={score_text}).", levels=levels
        )

    # ======================================================================
    # AOU-OSS
    # ======================================================================

    def _get_or_create_aou_runtime(
        self,
        symbol: str,
        inst: InstrumentConfig,
        session_date: date,
        lookback_df: pd.DataFrame,
    ) -> _AouRuntime:
        clean_symbol = str(symbol).strip().upper()
        with self._aou_runtimes_lock:
            runtime = self._aou_runtimes.get(clean_symbol)
            if runtime is not None and runtime.session_date == session_date:
                return runtime

            strategy = AouOssStrategy(inst, self.settings.strategy)
            if lookback_df is not None and not lookback_df.empty:
                strategy.seed_context(lookback_df)
            strategy.reset_session(session_date)

            runtime = _AouRuntime(strategy=strategy, session_date=session_date)
            self._aou_runtimes[clean_symbol] = runtime
            return runtime

    @staticmethod
    def _extract_best_bid_ask(book_snapshot: Any) -> Tuple[Optional[float], Optional[float]]:
        bids = getattr(book_snapshot, "bids", None)
        asks = getattr(book_snapshot, "asks", None)
        if not bids or not asks:
            return None, None

        def extract(levels: Any) -> Optional[float]:
            first = levels[0]
            if isinstance(first, dict):
                return _positive_number(first.get("price"))
            if isinstance(first, (list, tuple)) and first:
                return _positive_number(first[0])
            return _positive_number(getattr(first, "price", None))

        return extract(bids), extract(asks)

    def _evaluate_aou_oss(
        self,
        inst: InstrumentConfig,
        df_15m: pd.DataFrame,
        ltp: float,
        book_snapshot: Optional[Any] = None,
        allow_historical_session: bool = False,
    ) -> SingleStrategyPrediction:
        _, days = self._prepare_data(df_15m)
        if not days:
            return SingleStrategyPrediction("UNAVAILABLE", reason="No valid AOU-OSS session data.")

        now = now_ist_naive()
        today = now.date()
        if not allow_historical_session and not MarketCalendar.is_trading_day(today):
            return SingleStrategyPrediction(
                "UNAVAILABLE", reason="AOU-OSS is unavailable because today is not a trading session."
            )

        latest_date, today_df = days[-1]
        if not allow_historical_session and latest_date != today:
            return SingleStrategyPrediction(
                "UNAVAILABLE", reason=f"AOU-OSS live data is stale: latest session is {latest_date}, current session is {today}."
            )

        total_bars = sum(len(day_df) for _, day_df in days)
        if total_bars < 152:
            return SingleStrategyPrediction(
                "UNAVAILABLE", reason="Insufficient 15-minute history for AOU-OSS calibration."
            )

        lookback_df = (
            pd.concat([day_df for _, day_df in days[:-1]], ignore_index=True)
            if len(days) > 1 else pd.DataFrame()
        )
        runtime = self._get_or_create_aou_runtime(
            symbol=inst.symbol,
            inst=inst,
            session_date=latest_date,
            lookback_df=lookback_df,
        )

        best_bid, best_ask = (None, None)
        if book_snapshot is not None:
            best_bid, best_ask = self._extract_best_bid_ask(book_snapshot)

        with runtime.lock:
            strategy = runtime.strategy
            if best_bid is not None and best_ask is not None:
                strategy.set_market_context(
                    best_bid=best_bid,
                    best_ask=best_ask,
                )

            if runtime.last_candle_open is None:
                new_candles = today_df
            else:
                new_candles = today_df[today_df["datetime"] > runtime.last_candle_open]

            latest_signal = None
            for _, row in new_candles.iterrows():
                signal = strategy.on_candle(self._candle_dict(row), float(row["vwap"]))
                runtime.last_candle_open = row["datetime"]
                latest_signal = signal

            if not new_candles.empty:
                runtime.last_signal = latest_signal

            current_signal = runtime.last_signal
            state = strategy.get_state()

        levels = {
            "rolling_vwap": _finite_number(state.get("rolling_vwap")),
            "spread": _finite_number(state.get("spread")),
            "equilibrium": _finite_number(state.get("equilibrium")),
            "half_life_minutes": _finite_number(state.get("half_life_minutes")),
            "volatility_ratio": _finite_number(state.get("volatility_ratio")),
            "entry_boundary_long": _finite_number(state.get("entry_boundary_long")),
            "entry_boundary_short": _finite_number(state.get("entry_boundary_short")),
            "stop_boundary_long": _finite_number(state.get("stop_boundary_long")),
            "stop_boundary_short": _finite_number(state.get("stop_boundary_short")),
            "l2_spread_bps": _finite_number(state.get("l2_spread_bps")),
        }
        levels = {
            key: round(value, 4) if isinstance(value, float) else value
            for key, value in levels.items()
            if value is not None
        }

        if current_signal is not None and current_signal.action == SignalAction.BUY:
            return self._prediction_from_signal(
                "AOU_LONG", current_signal, ltp,
                "AOU-OSS optimal stopping generated a validated LONG signal.", levels
            )
        if current_signal is not None and current_signal.action == SignalAction.SELL:
            return self._prediction_from_signal(
                "AOU_SHORT", current_signal, ltp,
                "AOU-OSS optimal stopping generated a validated SHORT signal.", levels
            )

        if not state.get("ready"):
            half_life = _finite_number(state.get("half_life_minutes"))
            vol_ratio = _finite_number(state.get("volatility_ratio"))
            vol_passed = bool(state.get("volatility_passed", False))
            if half_life is not None and (half_life < 15.0 or half_life > 60.0):
                reason = f"AOU half-life ({half_life:.1f}m) outside 15-60m mean-reverting gate."
            elif not vol_passed and vol_ratio is not None:
                reason = f"AOU volatility ratio ({vol_ratio:.2f}) exceeds threshold."
            elif not bool(state.get("l2_passed", False)):
                reason = "AOU Level-2 spread gate failed."
            else:
                reason = "AOU-OSS calibration not ready or gates not satisfied."
            return SingleStrategyPrediction("NO_TRADE", reason=reason, levels=levels)

        spread_val = _finite_number(state.get("spread"))
        spread_text = f"{spread_val:.4f}" if spread_val is not None else "N/A"
        return SingleStrategyPrediction(
            "WAITING", reason=f"Awaiting AOU-OSS boundary trigger (spread={spread_text}).", levels=levels
        )

    # ======================================================================
    # SIGNAL EXPIRATION
    # ======================================================================

    @staticmethod
    def _no_trade_from(prediction: SingleStrategyPrediction, reason: str) -> SingleStrategyPrediction:
        return SingleStrategyPrediction(
            status="NO_TRADE",
            reason=reason,
            levels=prediction.levels,
            metrics=prediction.metrics,
            strategy=prediction.strategy,
            symbol=prediction.symbol,
            hedge_symbol=prediction.hedge_symbol,
            hedge_action=prediction.hedge_action,
            hedge_entry=prediction.hedge_entry,
            hedge_legs=prediction.hedge_legs,
            pair_prices=prediction.pair_prices,
            hedge_notional_weights=prediction.hedge_notional_weights,
        )

    @staticmethod
    def _invalidate_price_breached_signal(
        prediction: SingleStrategyPrediction,
        current_ltp: float,
    ) -> SingleStrategyPrediction:
        """Expire already-breached directional signals before publication."""
        if prediction.direction is None:
            return prediction

        current = _positive_number(current_ltp)
        if current is None:
            return PredictionService._no_trade_from(
                prediction, "Directional signal cannot be published without a valid live LTP."
            )

        stop = _positive_number(prediction.stop_loss)
        target = _positive_number(prediction.target)
        if stop is None or target is None:
            return PredictionService._no_trade_from(
                prediction, "Directional signal has invalid stop/target levels."
            )

        if prediction.direction == "LONG":
            if current <= stop:
                return PredictionService._no_trade_from(
                    prediction, "Signal expired: live LTP already crossed the stop."
                )
            if current >= target:
                return PredictionService._no_trade_from(
                    prediction, "Signal expired: live LTP already crossed the target."
                )
        elif prediction.direction == "SHORT":
            if current >= stop:
                return PredictionService._no_trade_from(
                    prediction, "Signal expired: live LTP already crossed the stop."
                )
            if current <= target:
                return PredictionService._no_trade_from(
                    prediction, "Signal expired: live LTP already crossed the target."
                )

        return prediction

    # ======================================================================
    # CONSENSUS
    # ======================================================================

    @classmethod
    def calculate_consensus(
        cls,
        predictions: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Equal-vote consensus over the complete set of seven single-name live strategies."""
        consensus_keys = [k for k in cls.LIVE_CONSENSUS_STRATEGIES if k in predictions]
        total_live = len(consensus_keys)

        normalized_predictions = {name: predictions[name] for name in consensus_keys}

        excluded_strategies = sorted(
            name for name in predictions if name not in cls.LIVE_CONSENSUS_STRATEGIES
        )

        base = {
            "total_strategies": total_live,
            "evaluable_strategies": 0,
            "directional_strategies": 0,
            "consensus_strategies": list(cls.LIVE_CONSENSUS_STRATEGIES),
            "excluded_strategies": excluded_strategies,
        }

        if total_live == 0:
            return {
                **base,
                "direction": "NEUTRAL",
                "agreeing_strategies": 0,
                "consensus_agreement_pct": None,
                "label": "UNAVAILABLE",
            }

        def _get_status(p: Any) -> str:
            if isinstance(p, dict):
                return str(p.get("status") or "")
            return str(getattr(p, "status", "") or "")

        def _get_direction(p: Any) -> Optional[str]:
            if isinstance(p, dict):
                return p.get("direction")
            return getattr(p, "direction", None)

        evaluable_count = sum(
            1
            for prediction in normalized_predictions.values()
            if _get_status(prediction) not in {"UNAVAILABLE", "ERROR", ""}
        )
        long_count = sum(
            1
            for prediction in normalized_predictions.values()
            if _get_status(prediction) not in {"UNAVAILABLE", "ERROR", ""}
            and _get_direction(prediction) == "LONG"
        )
        short_count = sum(
            1
            for prediction in normalized_predictions.values()
            if _get_status(prediction) not in {"UNAVAILABLE", "ERROR", ""}
            and _get_direction(prediction) == "SHORT"
        )
        directional_count = long_count + short_count

        base["evaluable_strategies"] = evaluable_count
        base["directional_strategies"] = directional_count

        if evaluable_count == 0:
            return {
                **base,
                "direction": "NEUTRAL",
                "agreeing_strategies": 0,
                "consensus_agreement_pct": None,
                "label": "UNAVAILABLE",
            }

        if directional_count == 0:
            return {
                **base,
                "direction": "NEUTRAL",
                "agreeing_strategies": 0,
                "consensus_agreement_pct": None,
                "label": "NEUTRAL",
            }

        if long_count > 0 and short_count > 0:
            direction = "DIVERGENT"
            agreeing = max(long_count, short_count)
            label = f"DIVERGENT ({long_count}L / {short_count}S)"
        elif long_count > 0:
            direction = "LONG"
            agreeing = long_count
            strength = "STRONG" if long_count >= 3 else "MODERATE" if long_count >= 2 else "WEAK"
            label = f"{strength} LONG ({long_count}/{total_live})"
        else:
            direction = "SHORT"
            agreeing = short_count
            strength = "STRONG" if short_count >= 3 else "MODERATE" if short_count >= 2 else "WEAK"
            label = f"{strength} SHORT ({short_count}/{total_live})"

        agreement_pct = round((agreeing / total_live) * 100.0, 1)
        return {
            **base,
            "direction": direction,
            "agreeing_strategies": agreeing,
            "consensus_agreement_pct": agreement_pct,
            "label": label,
        }

    # ======================================================================
    # INSIGHTS
    # ======================================================================

    @staticmethod
    def extract_key_insights(candidates: List[CandidatePrediction]) -> Dict[str, Any]:
        if not candidates:
            return {
                "top_long": None,
                "top_short": None,
                "strongest_consensus": None,
                "divergent_signals": [],
            }

        long_candidates = [
            candidate for candidate in candidates
            if candidate.consensus.get("direction") == "LONG"
        ]
        short_candidates = [
            candidate for candidate in candidates
            if candidate.consensus.get("direction") == "SHORT"
        ]
        directional_candidates = [
            candidate for candidate in candidates
            if candidate.consensus.get("direction") in {"LONG", "SHORT"}
        ]

        def consensus_key(candidate: CandidatePrediction) -> Tuple[int, float]:
            return (
                int(candidate.consensus.get("agreeing_strategies", 0)),
                float(candidate.momentum_score),
            )

        top_long = max(long_candidates, key=consensus_key, default=None)
        top_short = max(short_candidates, key=consensus_key, default=None)
        strongest = max(directional_candidates, key=consensus_key, default=None)
        divergent = [
            candidate.to_dict()
            for candidate in candidates
            if candidate.consensus.get("direction") == "DIVERGENT"
        ]

        return {
            "top_long": top_long.to_dict() if top_long is not None else None,
            "top_short": top_short.to_dict() if top_short is not None else None,
            "strongest_consensus": strongest.to_dict() if strongest is not None else None,
            "divergent_signals": divergent,
        }


prediction_service = PredictionService()
