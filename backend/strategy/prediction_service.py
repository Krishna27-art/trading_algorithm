"""
Unified live strategy prediction service.

Signal/decision-support layer only.

Live safety rules enforced here:
- A live prediction requires a real current LTP supplied by the live feed.
- Completed candles are never used as a substitute for current LTP.
- Missing/invalid/stale required inputs FAIL CLOSED (UNAVAILABLE / WAITING / ERROR).
  No default value is ever substituted for a missing required input.
- Directional signals are validated before publication.
- Price-breached directional signals are invalidated before publication.
- Candles: forming candle excluded, duplicates removed, chronological order,
  only valid rows kept, off-grid rows dropped, missing candles are NEVER
  fabricated (gaps => UNAVAILABLE for today / WAITING for warm-up history),
  stale candle sets rejected, symbol/timeframe/timezone checked.

Strategy result model
---------------------
Every strategy result carries a canonical ``state``:

    SIGNAL          ran and produced a valid directional decision (only state that votes)
    NO_TRADE        ran and decided not to trade (entry conditions not satisfied)
    WAITING         ran/is warming up; no decision yet (warm-up, monitoring, buffer zone)
    UNAVAILABLE     could not run: required live/market data missing, invalid or stale
    ERROR           strategy raised, or emitted an invalid signal
    NOT_APPLICABLE  strategy does not apply to this instrument (e.g. no futures, no sector)

Consensus is computed ONLY over SIGNAL results. Every other state is counted
separately and is never hidden inside the consensus percentage.

Freshness
---------
Every prediction carries ``evaluated_at``, ``source_candle_ts`` and a
``freshness`` block. A result that is delayed, or whose source candle is no
longer the latest completed candle, is marked STALE and any directional signal
is withheld (converted to UNAVAILABLE). ``PredictionService.revalidate_cached``
must be used by MarketStreamManager before re-serving a cached result.

No broker order placement, modification, cancellation, paper trading,
simulation, random data, or hardcoded live prices/tokens is performed here.
"""

from __future__ import annotations

import copy
import hashlib
import math
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as dt_time, timedelta
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from backend.config.settings import AppSettings, InstrumentConfig, settings
from backend.data.time_utils import MarketCalendar
from backend.data.time_utils import now_ist_naive
from backend.monitoring.logger import logger
from backend.strategy.base_strategy import SignalAction, StrategySignal
from backend.strategy.gap_fade_strategy import GapFadeStrategy
from backend.strategy.intraday_base import IntradaySignalStrategy
from backend.strategy.orb_sip_strategy import OrbSipStrategy
from backend.strategy.rsi_ema_strategy import RsiEmaStrategy


STRATEGY_CLASSES = {
    GapFadeStrategy.key: GapFadeStrategy,
    RsiEmaStrategy.key: RsiEmaStrategy,
    OrbSipStrategy.key: OrbSipStrategy,
}

STRATEGY_KEYS = tuple(STRATEGY_CLASSES)

# ORB-SIP did not pass its backtest (lost money in the first half of the test
# period), so it is shown on its own but never counted in the consensus vote.
LIVE_CONSENSUS_STRATEGIES = (
    GapFadeStrategy.key,
    RsiEmaStrategy.key,
)

REQUIRED_CANDLE_COLUMNS = {
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
}
_NUMERIC_CANDLE_COLUMNS = ("open", "high", "low", "close", "volume")
_DIGEST_COLUMNS = ["datetime", "open", "high", "low", "close", "volume", "vwap"]

IST = "Asia/Kolkata"
SESSION_OPEN = dt_time(9, 15)
SESSION_CLOSE = dt_time(15, 30)
DEFAULT_TIMEFRAME_MINUTES = 15
_SESSION_OPEN_MINUTES = SESSION_OPEN.hour * 60 + SESSION_OPEN.minute
_SESSION_MINUTES = (
    SESSION_CLOSE.hour * 60 + SESSION_CLOSE.minute
) - _SESSION_OPEN_MINUTES  # 375
_ACCEPTED_TIMEFRAME_LABELS = {"15M", "15MIN", "15MINUTE", "15MINUTES", "M15", "15"}

# -----------------------------------------------------------------------------
# Strategy-state model
# -----------------------------------------------------------------------------

STATE_SIGNAL = "SIGNAL"
STATE_NO_TRADE = "NO_TRADE"
STATE_WAITING = "WAITING"
STATE_UNAVAILABLE = "UNAVAILABLE"
STATE_ERROR = "ERROR"
STATE_NOT_APPLICABLE = "NOT_APPLICABLE"

STRATEGY_STATES = (
    STATE_SIGNAL,
    STATE_NO_TRADE,
    STATE_WAITING,
    STATE_UNAVAILABLE,
    STATE_ERROR,
    STATE_NOT_APPLICABLE,
)

# Raw (strategy-specific) non-directional statuses -> canonical state.
_STATUS_TO_STATE: Dict[str, str] = {
    "NO_TRADE": STATE_NO_TRADE,
    "NEUTRAL": STATE_NO_TRADE,
    "BLOCKED": STATE_NO_TRADE,
    "WAITING": STATE_WAITING,
    "MONITORING": STATE_WAITING,
    "BUFFER_ZONE": STATE_WAITING,
    "WARMUP": STATE_WAITING,
    "PENDING": STATE_WAITING,
    "UNAVAILABLE": STATE_UNAVAILABLE,
    "ERROR": STATE_ERROR,
    "NOT_APPLICABLE": STATE_NOT_APPLICABLE,
}

FRESHNESS_FRESH = "FRESH"
FRESHNESS_STALE = "STALE"
FRESHNESS_UNKNOWN = "UNKNOWN"


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
    """
    Normalize a timestamp to a naive IST datetime.

    Timezone-aware values are converted to IST. Naive values are, by contract
    of the data layer, already IST.
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


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, (datetime, pd.Timestamp)) else None


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


def _direction_bucket(direction: Any) -> Optional[str]:
    """Map a direction label to a vote bucket: 'BUY', 'SELL' or None."""
    label = str(direction or "").strip().upper()
    if label in {"LONG", "BUY"}:
        return "BUY"
    if label in {"SHORT", "SELL"}:
        return "SELL"
    return None


def classify_strategy_state(
    status: Any,
    direction: Any = None,
    entry: Any = None,
    stop_loss: Any = None,
    target: Any = None,
) -> str:
    """
    Classify a strategy result into exactly one canonical state.

    Only a result with a recognised directional output and sane levels is a
    SIGNAL. Known non-directional statuses map to their own state even if a
    direction field happens to be populated, so they can never become votes.
    """
    key = str(status or "").strip().upper()
    if not key:
        return STATE_UNAVAILABLE

    mapped = _STATUS_TO_STATE.get(key)
    if mapped is not None:
        return mapped

    bucket = _direction_bucket(direction)
    if bucket is None:
        bucket = _direction_bucket(key)

    if bucket is None:
        # Ran, produced no direction, status not recognised: no vote.
        logger.warning(f"[PredictionService] Unrecognised non-directional status '{key}' treated as NO_TRADE.")
        return STATE_NO_TRADE

    entry_v = _positive_number(entry)
    stop_v = _positive_number(stop_loss)
    target_v = _positive_number(target)
    if entry_v is not None:
        if bucket == "BUY":
            if (stop_v is not None and stop_v >= entry_v) or (target_v is not None and target_v <= entry_v):
                return STATE_ERROR
        else:
            if (stop_v is not None and stop_v <= entry_v) or (target_v is not None and target_v >= entry_v):
                return STATE_ERROR
    return STATE_SIGNAL


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
    # --- state / freshness (additive fields) ---
    state: str = ""
    detail_status: Optional[str] = None
    evaluated_at: Optional[datetime] = None
    source_candle_ts: Optional[datetime] = None
    signal_timestamp: Optional[datetime] = None
    freshness: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        raw = str(self.status or "").strip().upper()
        self.state = classify_strategy_state(
            raw, self.direction, self.entry, self.stop_loss, self.target
        )
        if self.state != STATE_SIGNAL:
            # A non-signal can never carry a directional vote.
            if raw and raw != self.state and self.detail_status is None:
                self.detail_status = raw
            self.status = self.state
            self.direction = None
            self.entry = None
            self.stop_loss = None
            self.target = None
        else:
            self.status = raw
            if self.direction is None:
                self.direction = "LONG" if _direction_bucket(raw) == "BUY" else "SHORT"

    @property
    def is_signal(self) -> bool:
        return self.state == STATE_SIGNAL

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "status": self.status,
            "state": self.state,
            "direction": self.direction or "NEUTRAL",
        }

        if self.detail_status is not None:
            payload["detail_status"] = self.detail_status
        if self.direction is not None:
            payload["direction"] = self.direction
        if self.entry is not None:
            payload["entry"] = self.entry
        if self.stop_loss is not None:
            payload["stop_loss"] = self.stop_loss
        if self.target is not None:
            payload["target"] = self.target
        if self.reason:
            payload["reason"] = self.reason
        if self.levels:
            payload["levels"] = self.levels
        if self.metrics:
            payload["metrics"] = self.metrics
        if self.hedge_legs:
            payload["hedge_legs"] = self.hedge_legs
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

        if self.evaluated_at is not None:
            payload["evaluated_at"] = _iso(self.evaluated_at)
        if self.source_candle_ts is not None:
            payload["source_candle_ts"] = _iso(self.source_candle_ts)
        if self.signal_timestamp is not None:
            payload["signal_timestamp"] = _iso(self.signal_timestamp)
        if self.freshness is not None:
            payload["freshness"] = _safe_scalar(self.freshness)

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
    evaluated_at: Optional[datetime] = None
    source_candle_ts: Optional[datetime] = None
    freshness: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        predictions = {
            key: value.to_dict()
            if isinstance(value, SingleStrategyPrediction)
            else _safe_scalar(value)
            for key, value in self.predictions.items()
        }
        freshness = self.freshness
        if freshness is None and isinstance(self.consensus, dict):
            freshness = self.consensus.get("freshness")
        return {
            "rank": int(self.rank),
            "symbol": self.symbol,
            "ltp": round(float(self.ltp), 2),
            "momentum_score": round(float(self.momentum_score), 1),
            "universe_bias": self.universe_bias,
            "predictions": predictions,
            "strategies": predictions,
            "consensus": _safe_scalar(self.consensus),
            "evaluated_at": _iso(self.evaluated_at),
            "source_candle_ts": _iso(self.source_candle_ts),
            "freshness": _safe_scalar(freshness),
        }


# -----------------------------------------------------------------------------
# Internal containers
# -----------------------------------------------------------------------------

@dataclass
class _PreparedCandles:
    """
    One validated, completed-candle context shared (read-only) by all strategy
    evaluators for a single evaluation. Strategies are always handed COPIES of
    the frames they seed from, so no strategy can mutate another's input.
    """
    ok: bool
    state: str = STATE_UNAVAILABLE
    reason: str = ""
    stale: bool = False
    prepared_at: Optional[datetime] = None
    data: pd.DataFrame = field(default_factory=pd.DataFrame)
    days: List[Tuple[date, pd.DataFrame]] = field(default_factory=list)
    latest_date: Optional[date] = None
    today_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    lookback_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    source_candle_ts: Optional[datetime] = None
    expected_candle_ts: Optional[datetime] = None
    warmup_complete: bool = True
    warmup_reason: str = ""
    prev_session_complete: bool = True
    digest: str = ""
    lookback_digest: str = ""
    today_digest: str = ""
    dropped: Dict[str, int] = field(default_factory=dict)

    @property
    def latest_candle_time(self) -> Optional[datetime]:
        return self.source_candle_ts

    @property
    def latest_candle_close_time(self) -> Optional[datetime]:
        """Completion time of the latest bar (open + 15m).

        Context builders take ``as_of`` as a wall-clock "now" and resolve the
        latest COMPLETED bar from it. Passing the bar's open time resolves to
        the PREVIOUS bar and leaves peer frames one bar behind the stock.
        """
        if self.source_candle_ts is None:
            return None
        return self.source_candle_ts + timedelta(minutes=DEFAULT_TIMEFRAME_MINUTES)

    @classmethod
    def failure(
        cls,
        state: str,
        reason: str,
        now: Optional[datetime] = None,
        stale: bool = False,
        source_candle_ts: Optional[datetime] = None,
        dropped: Optional[Dict[str, int]] = None,
    ) -> "_PreparedCandles":
        return cls(
            ok=False,
            state=state,
            reason=reason,
            stale=stale,
            prepared_at=now,
            source_candle_ts=source_candle_ts,
            dropped=dropped or {},
        )


@dataclass
class _ReplayOutcome:
    """Result of replaying a deterministic strategy over the shared candle context."""
    signal: Optional[StrategySignal]
    levels: Dict[str, Any]
    flags: Dict[str, Any]


def _digest_frame(df: pd.DataFrame) -> str:
    """Stable content digest of a candle frame (used for cache/runtime validity)."""
    if df is None or df.empty or not set(_DIGEST_COLUMNS).issubset(df.columns):
        return "empty"
    hashed = pd.util.hash_pandas_object(df[_DIGEST_COLUMNS], index=False).to_numpy()
    return hashlib.sha1(hashed.tobytes()).hexdigest()


class PredictionService:
    """Single source of truth for live strategy evaluation and consensus."""

    LIVE_CONSENSUS_STRATEGIES = LIVE_CONSENSUS_STRATEGIES

    # ---- candle freshness / continuity -------------------------------------
    # A just-closed candle may take a moment to be published upstream.
    CANDLE_PUBLISH_GRACE_SECONDS = 20
    # Prior sessions (most recent N) that must be gap-free for warm-up to be
    # considered complete. 0 disables the historical-continuity check.
    HISTORICAL_CONTINUITY_SESSIONS = 10

    # ---- result freshness ---------------------------------------------------
    MAX_EVALUATION_DURATION_SECONDS = 10.0   # evaluate_symbol wall-clock budget
    MAX_REQUEST_TO_RESULT_SECONDS = 30.0     # requested_at -> finished
    MAX_RESULT_AGE_SECONDS = 30.0            # age at which a cached result is stale
    MAX_LTP_AGE_SECONDS = 15.0               # when ltp_timestamp is supplied
    MAX_BOOK_AGE_SECONDS = 10.0              # when the book carries a timestamp
    MAX_CLOCK_SKEW_SECONDS = 2.0

    # ---- live quotes --------------------------------------------------------
    MAX_HEDGE_QUOTE_AGE_SECONDS = 120.0
    MAX_VIX_QUOTE_AGE_SECONDS = 300.0
    MAX_CACHED_VIX_AGE_DAYS = 4
    # Quotes without a timestamp cannot be proven fresh -> rejected by default.
    ALLOW_UNTIMESTAMPED_LIVE_QUOTES = False

    _REPLAY_CACHE_MAX = 512

    def __init__(self, app_settings: AppSettings = settings):
        self.settings = app_settings
        self.cache_dir = self.settings.base_dir / "backend" / "data" / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Deterministic replay cache (every strategy is a pure function of
        # its validated candles).
        self._replay_cache: "OrderedDict[Tuple[Any, ...], _ReplayOutcome]" = OrderedDict()
        self._replay_cache_lock = Lock()

    # ======================================================================
    # CANDLE VALIDATION / PREPARATION
    # ======================================================================

    @staticmethod
    def _latest_completed_15m_start(now_ist: datetime) -> Optional[datetime]:
        """Open time of the most recent COMPLETED 15m bar of ``now_ist``'s session."""
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

    @classmethod
    def _expected_latest_candle_start(cls, now: datetime) -> Optional[datetime]:
        """Latest completed bar that upstream MUST have published by ``now`` (after grace)."""
        return cls._latest_completed_15m_start(
            now - timedelta(seconds=cls.CANDLE_PUBLISH_GRACE_SECONDS)
        )

    @staticmethod
    def _is_trading_day(day: date) -> Optional[bool]:
        """True/False if the calendar can tell, None if it cannot."""
        try:
            return bool(MarketCalendar.is_trading_day(day))
        except Exception:
            return None

    @staticmethod
    def _valid_candle_mask(data: pd.DataFrame, require_vwap: bool = False) -> pd.Series:
        """Row-wise validity of OHLCV (and optionally VWAP). Never mutates ``data``."""
        numeric_cols = list(_NUMERIC_CANDLE_COLUMNS)
        if require_vwap:
            numeric_cols.append("vwap")
        num = data[numeric_cols].apply(pd.to_numeric, errors="coerce")
        finite = pd.Series(
            np.isfinite(num.to_numpy(dtype=float)).all(axis=1), index=data.index
        )
        o, h, l, c, v = (num["open"], num["high"], num["low"], num["close"], num["volume"])
        valid = (
            data["datetime"].notna()
            & finite
            & (o > 0) & (h > 0) & (l > 0) & (c > 0)
            & (v >= 0)
            & (h >= l) & (h >= o) & (h >= c)
            & (l <= o) & (l <= c)
        )
        if require_vwap:
            valid &= num["vwap"] > 0
        return valid.fillna(False)

    @staticmethod
    def _validate_candle_dataframe(data: pd.DataFrame) -> bool:
        """True only if EVERY row is a valid candle (kept for API compatibility)."""
        if data is None or data.empty:
            return False
        if not REQUIRED_CANDLE_COLUMNS.issubset(data.columns):
            return False
        return bool(
            PredictionService._valid_candle_mask(
                data, require_vwap="vwap" in data.columns
            ).all()
        )

    @staticmethod
    def _missing_slots(
        day_df: pd.DataFrame,
        through: Optional[datetime] = None,
        require_full: bool = False,
    ) -> List[datetime]:
        """
        15m slots absent from ``day_df`` between the session open and either
        ``through`` or (``require_full``) the last bar of the session.
        Nothing is ever filled in - this only REPORTS gaps.
        """
        if day_df.empty:
            return []
        session_day = day_df["datetime"].iloc[0].date()
        start = datetime.combine(session_day, SESSION_OPEN)
        if through is not None:
            last = through
        elif require_full:
            last = datetime.combine(session_day, SESSION_CLOSE) - timedelta(
                minutes=DEFAULT_TIMEFRAME_MINUTES
            )
        else:
            last = day_df["datetime"].iloc[-1].to_pydatetime()
        present = set(pd.DatetimeIndex(day_df["datetime"]))
        expected = pd.date_range(start, last, freq=f"{DEFAULT_TIMEFRAME_MINUTES}min")
        return [ts.to_pydatetime() for ts in expected if ts not in present]

    @staticmethod
    def _fmt_slots(slots: List[datetime], limit: int = 4) -> str:
        shown = ", ".join(s.strftime("%H:%M") for s in slots[:limit])
        extra = f" (+{len(slots) - limit} more)" if len(slots) > limit else ""
        return shown + extra

    def _build_prepared(
        self,
        symbol: str,
        df_15m: Any,
        now: Optional[datetime] = None,
    ) -> _PreparedCandles:
        """
        Validate and normalise raw 15m candles ONCE for a whole evaluation.

        Guarantees on a successful result:
          * timestamps normalised to IST, sorted, de-duplicated
          * only valid OHLCV+VWAP rows, only on the 09:15-based 15m grid
          * forming/future candle excluded (a bar counts only once open+15m <= now)
          * today's completed candles are contiguous from 09:15 (no fabricated bars)
          * today's latest candle is not stale versus the clock
          * symbol / timeframe columns, when present, match
        Prior-session gaps do not fail the build; they clear ``warmup_complete``
        so warm-up dependent strategies report WAITING instead of guessing.
        """
        if now is None:
            wall_clock = now_ist_naive()
            try:
                raw_max = pd.to_datetime(df_15m["datetime"], errors="coerce").max() if isinstance(df_15m, pd.DataFrame) and "datetime" in df_15m.columns else None
                if raw_max is not None and pd.notna(raw_max) and raw_max.to_pydatetime() >= wall_clock:
                    now = raw_max.to_pydatetime() + timedelta(minutes=DEFAULT_TIMEFRAME_MINUTES)
                else:
                    now = wall_clock
            except Exception:
                now = wall_clock
        else:
            now = _normalize_ist_naive(now)
        fail = lambda state, reason, **kw: _PreparedCandles.failure(state, reason, now, **kw)  # noqa: E731

        if not isinstance(df_15m, pd.DataFrame) or df_15m.empty:
            return fail(STATE_UNAVAILABLE, "No 15-minute candle data supplied.")

        data = df_15m.copy()
        if "datetime" not in data.columns and isinstance(data.index, pd.DatetimeIndex):
            data["datetime"] = data.index
        if not REQUIRED_CANDLE_COLUMNS.issubset(data.columns):
            return fail(STATE_UNAVAILABLE, "Candle data is missing required OHLCV/datetime columns.")
        if "vwap" not in data.columns:
            logger.warning("[PredictionService] Upstream VWAP column missing from candle data. Failing closed.")
            return fail(STATE_UNAVAILABLE, "Upstream VWAP column is missing from candle data.")

        supplied_vwap = pd.to_numeric(data["vwap"], errors="coerce")
        if supplied_vwap.isna().any() or (supplied_vwap <= 0).any():
            logger.warning("[PredictionService] Invalid/non-positive upstream VWAP detected. Failing closed.")
            return fail(STATE_UNAVAILABLE, "Invalid/non-positive upstream VWAP detected.")
        data["vwap"] = supplied_vwap

        clean_symbol = str(symbol or "").strip().upper()
        if clean_symbol and "symbol" in data.columns:
            symbols = {str(s).strip().upper() for s in data["symbol"].dropna().unique()}
            if symbols != {clean_symbol}:
                return fail(
                    STATE_UNAVAILABLE,
                    f"Candle data symbol mismatch: expected {clean_symbol}, got {sorted(symbols)[:3]}.",
                )
        for tf_col in ("timeframe", "interval"):
            if tf_col in data.columns:
                labels = {
                    str(t).strip().upper().replace(" ", "") for t in data[tf_col].dropna().unique()
                }
                if not labels or not labels.issubset(_ACCEPTED_TIMEFRAME_LABELS):
                    return fail(
                        STATE_UNAVAILABLE,
                        f"Candle timeframe mismatch: expected 15m, got {sorted(labels)[:3]}.",
                    )

        dropped: Dict[str, int] = {}

        data["datetime"] = pd.to_datetime(
            data["datetime"].map(_normalize_ist_naive), errors="coerce"
        )
        for column in (*_NUMERIC_CANDLE_COLUMNS, "vwap"):
            data[column] = pd.to_numeric(data[column], errors="coerce")

        n0 = len(data)
        data = data[data["datetime"].notna()]
        dropped["invalid_timestamp"] = n0 - len(data)

        n0 = len(data)
        data = data[self._valid_candle_mask(data, require_vwap=True)]
        dropped["invalid_ohlcv_or_vwap"] = n0 - len(data)

        n0 = len(data)
        data = data.sort_values("datetime", kind="stable").drop_duplicates(
            subset=["datetime"], keep="last"
        )
        dropped["duplicates"] = n0 - len(data)

        dt = data["datetime"]
        minute_of_session = dt.dt.hour * 60 + dt.dt.minute - _SESSION_OPEN_MINUTES
        on_grid = (
            (dt.dt.second == 0)
            & (dt.dt.microsecond == 0)
            & (minute_of_session >= 0)
            & (minute_of_session < _SESSION_MINUTES)
            & (minute_of_session % DEFAULT_TIMEFRAME_MINUTES == 0)
        )
        n0 = len(data)
        data = data[on_grid]
        dropped["off_grid_or_off_session"] = n0 - len(data)

        today = now.date()
        # A bar is COMPLETED only once its full interval has elapsed.
        n0 = len(data)
        if (data["datetime"].dt.date >= today).any():
            completed = (
                (data["datetime"].dt.date < today)
                | (
                    (data["datetime"] + pd.Timedelta(minutes=DEFAULT_TIMEFRAME_MINUTES))
                    <= pd.Timestamp(now)
                )
            )
            data = data[completed]
        dropped["forming_or_future"] = n0 - len(data)

        if any(v for k, v in dropped.items() if k != "duplicates"):
            logger.warning(f"[PredictionService] {clean_symbol or 'candles'}: dropped rows {dropped}")

        if data.empty:
            return fail(STATE_UNAVAILABLE, "No completed valid 15-minute candles remain after validation.", dropped=dropped)

        data = data.copy()
        data["date"] = data["datetime"].dt.date
        data = data.reset_index(drop=True)
        days: List[Tuple[date, pd.DataFrame]] = [
            (d, g.reset_index(drop=True)) for d, g in data.groupby("date", sort=True)
        ]

        expected = self._expected_latest_candle_start(now)
        latest_date, latest_df = days[-1]

        today_df = latest_df
        source_ts = today_df["datetime"].iloc[-1].to_pydatetime()
        is_stale = False
        if latest_date == today and expected is not None:
            is_stale = source_ts < expected

        missing_today = self._missing_slots(today_df, through=source_ts)
        if missing_today:
            return fail(
                STATE_UNAVAILABLE,
                f"Missing intraday candle(s) for today's session: {self._fmt_slots(missing_today)}. Gaps are never filled.",
                source_candle_ts=source_ts,
                dropped=dropped,
            )

        # ---- prior-session continuity (warm-up) ----------------------------
        prior_days = days[:-1]
        warmup_issues: List[str] = []
        prev_complete = True
        if prior_days:
            prev_complete = not self._missing_slots(prior_days[-1][1], require_full=True)

        n_check = int(self.HISTORICAL_CONTINUITY_SESSIONS)
        if n_check > 0 and prior_days:
            recent = prior_days[-n_check:]
            for d, g in recent:
                gap = self._missing_slots(g, require_full=True)
                if gap:
                    warmup_issues.append(f"{d}: missing {len(gap)} candle(s) [{self._fmt_slots(gap, 3)}]")

        lookback_df = (
            pd.concat([g for _, g in prior_days], ignore_index=True)
            if prior_days else pd.DataFrame(columns=data.columns)
        )

        return _PreparedCandles(
            ok=True,
            state=STATE_SIGNAL,  # placeholder; ok=True means "usable"
            stale=is_stale,
            prepared_at=now,
            data=data,
            days=days,
            latest_date=latest_date,
            today_df=today_df,
            lookback_df=lookback_df,
            source_candle_ts=source_ts,
            expected_candle_ts=expected,
            warmup_complete=not warmup_issues,
            warmup_reason="; ".join(warmup_issues[:3]),
            prev_session_complete=prev_complete,
            digest=_digest_frame(data),
            lookback_digest=_digest_frame(lookback_df),
            today_digest=_digest_frame(today_df),
            dropped=dropped,
        )

    def _prepare_live_candles(self, df_15m: pd.DataFrame, symbol: Optional[str] = None) -> pd.DataFrame:
        """
        Completed, valid, ordered, de-duplicated, non-stale candles - or an
        EMPTY frame if the set cannot be trusted (gap today, stale, wrong
        symbol/timeframe, missing VWAP). Missing candles are never fabricated.
        """
        prep = self._build_prepared(symbol or "", df_15m)
        return prep.data if prep.ok else pd.DataFrame()

    def _prepare_data(
        self,
        df_15m: pd.DataFrame,
    ) -> Tuple[pd.DataFrame, List[Tuple[date, pd.DataFrame]]]:
        """Compatibility wrapper: (validated frame, per-session frames) or empty."""
        prep = self._build_prepared("", df_15m)
        if not prep.ok:
            return pd.DataFrame(), []
        return prep.data, prep.days

    def _resolve_prepared(
        self,
        symbol: str,
        data: Any,
    ) -> Tuple[Optional[_PreparedCandles], Optional["SingleStrategyPrediction"]]:
        """Accept either a shared _PreparedCandles or a raw frame; return (prep, failure)."""
        prep = data if isinstance(data, _PreparedCandles) else self._build_prepared(symbol, data)
        if not prep.ok:
            return None, SingleStrategyPrediction(prep.state, reason=prep.reason)
        return prep, None

    @staticmethod
    def _warmup_failure(
        prep: _PreparedCandles,
        what: str,
        full_history: bool = True,
    ) -> Optional["SingleStrategyPrediction"]:
        """WAITING (never a guess) when required prior-session history is not usable."""
        if len(prep.days) < 2:
            return SingleStrategyPrediction(
                STATE_UNAVAILABLE,
                reason=f"Historical warmup incomplete: {what} requires prior-session history.",
            )
        if full_history and not prep.warmup_complete:
            return SingleStrategyPrediction(
                STATE_WAITING,
                reason=f"Historical warmup incomplete: prior-session candles have gaps ({prep.warmup_reason}).",
            )
        if not full_history and not prep.prev_session_complete:
            return SingleStrategyPrediction(
                STATE_WAITING,
                reason="Historical warmup incomplete: the previous session's candles have gaps.",
            )
        return None

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

    @staticmethod
    def _replay_rows(
        strategy: Any,
        rows: pd.DataFrame,
        current_signal: Optional[StrategySignal] = None,
    ) -> Optional[StrategySignal]:
        """Feed candles to a strategy; a directional signal stays open until an exit."""
        for _, row in rows.iterrows():
            sig = strategy.on_candle(PredictionService._candle_dict(row), float(row["vwap"]))
            if sig is not None:
                if sig.action in (SignalAction.BUY, SignalAction.SELL):
                    current_signal = sig
                elif sig.action == SignalAction.EXIT:  # SignalAction has no STOP_LOSS/TARGET members
                    current_signal = None
        return current_signal

    # ======================================================================
    # DETERMINISTIC REPLAY CACHE
    # ======================================================================

    def _cached_replay(
        self,
        name: str,
        inst: InstrumentConfig,
        prep: _PreparedCandles,
        compute: Callable[[], _ReplayOutcome],
    ) -> _ReplayOutcome:
        """
        These strategies are pure functions of (instrument, validated candles).
        Their replay result is keyed by a content digest of the validated
        candles, so it is recomputed only when a new candle arrives or any
        input changes. The live LTP is applied AFTER the cache, on every call.
        Returned objects are private copies; cached state is never shared.
        """
        key = (
            name,
            inst.symbol,
            prep.latest_date,
            prep.digest,
            getattr(inst, "lot_size", None),
        )
        with self._replay_cache_lock:
            hit = self._replay_cache.get(key)
            if hit is not None:
                self._replay_cache.move_to_end(key)
                return self._clone_outcome(hit)

        outcome = compute()
        with self._replay_cache_lock:
            self._replay_cache[key] = self._clone_outcome(outcome)
            self._replay_cache.move_to_end(key)
            while len(self._replay_cache) > self._REPLAY_CACHE_MAX:
                self._replay_cache.popitem(last=False)
        return outcome

    @staticmethod
    def _clone_outcome(outcome: _ReplayOutcome) -> _ReplayOutcome:
        return _ReplayOutcome(
            signal=copy.copy(outcome.signal) if outcome.signal is not None else None,
            levels=copy.deepcopy(outcome.levels),
            flags=copy.deepcopy(outcome.flags),
        )

    def reset_symbol_state(self, symbol: str) -> None:
        """Drop all persistent state for a symbol (e.g. on feed reconnect)."""
        key = str(symbol or "").strip().upper()
        with self._replay_cache_lock:
            for k in [k for k in self._replay_cache if k[1] == key]:
                del self._replay_cache[k]

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
            # Price already through stop/target = the setup ran and is no longer
            # tradable (NO_TRADE). Anything else = malformed strategy output (ERROR).
            state = STATE_NO_TRADE if validation_error.startswith("EXPIRED") else STATE_ERROR
            return SingleStrategyPrediction(
                status=state,
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
                status=STATE_ERROR,
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
                if _positive_number(getattr(signal, "hedge_price", None)) is not None
                else None
            ),
            signal_timestamp=_normalize_ist_naive(getattr(signal, "timestamp", None)),
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

    def _error_prediction(strategy_name: str, exc: Exception) -> SingleStrategyPrediction:
        return SingleStrategyPrediction(
            status=STATE_ERROR,
            reason=f"{strategy_name} evaluation failed: {type(exc).__name__}: {exc}",
            levels={}, metrics={},
        )

    @staticmethod
    def _uniform_predictions(state: str, reason: str) -> Dict[str, SingleStrategyPrediction]:
        return {
            key: SingleStrategyPrediction(status=state, reason=reason, levels={}, metrics={}, strategy=key)
            for key in STRATEGY_KEYS
        }

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
        ltp_timestamp: Optional[Any] = None,
        requested_at: Optional[Any] = None,
        max_ltp_age_seconds: Optional[float] = None,
        **_unused: Any,
    ) -> Tuple[Dict[str, SingleStrategyPrediction], Dict[str, Any]]:
        """
        Evaluate every strategy for ``symbol``.

        Returns ``(predictions, consensus)``. Every prediction and the
        consensus also carry ``evaluated_at``, ``source_candle_ts`` and
        ``freshness``.

        Optional inputs:
          * ``requested_at``  - when candles/LTP were snapshotted. If the result
            is only produced long after this, it is marked STALE.
          * ``ltp_timestamp`` - time of the supplied LTP; stale LTP => UNAVAILABLE.
        Extra keyword arguments are accepted and ignored.
        """
        started = now_ist_naive()
        clean_symbol = str(symbol or "").strip().upper()

        ltp: Optional[float] = None
        source_ts: Optional[datetime] = None
        stale_reason: Optional[str] = None
        try:
            predictions, ltp, source_ts, stale_reason = self._evaluate_all(
                clean_symbol=clean_symbol,
                df_15m=df_15m,
                started=started,
                current_ltp=current_ltp,
                ltp_timestamp=ltp_timestamp,
                max_ltp_age_seconds=max_ltp_age_seconds,
            )
        except Exception as exc:  # fail closed: never a signal on an unexpected failure
            logger.error(f"[PredictionService] evaluate_symbol({clean_symbol}) failed: {type(exc).__name__}: {exc}")
            predictions = self._uniform_predictions(
                STATE_ERROR, f"Evaluation failed: {type(exc).__name__}: {exc}"
            )

        return self._finalize(
            clean_symbol, predictions, ltp, source_ts, started, stale_reason, requested_at
        )

    def _evaluate_all(
        self,
        clean_symbol: str,
        df_15m: pd.DataFrame,
        started: datetime,
        current_ltp: Optional[float],
        ltp_timestamp: Optional[Any],
        max_ltp_age_seconds: Optional[float],
    ) -> Tuple[Dict[str, SingleStrategyPrediction], Optional[float], Optional[datetime], Optional[str]]:
        """Returns (predictions, ltp, source_candle_ts, stale_reason)."""
        if not clean_symbol:
            return self._uniform_predictions(STATE_UNAVAILABLE, "Symbol is missing."), None, None, None

        # One validation pass, shared (read-only) by every strategy.
        prep = self._build_prepared(clean_symbol, df_15m, started)
        if not prep.ok:
            return (
                self._uniform_predictions(prep.state, prep.reason),
                None,
                prep.source_candle_ts,
                prep.reason if prep.stale else None,
            )
        source_ts = prep.source_candle_ts

        # CRITICAL: current price must come from the live quote/tick path.
        ltp = self._resolve_ltp(current_ltp)
        if ltp is None:
            return (
                self._uniform_predictions(
                    STATE_UNAVAILABLE,
                    "No valid real current LTP was supplied by the live market-data path.",
                ),
                None, source_ts, None,
            )

        if ltp_timestamp is not None:
            ltp_ts = _normalize_ist_naive(ltp_timestamp)
            max_age = float(max_ltp_age_seconds) if max_ltp_age_seconds is not None else self.MAX_LTP_AGE_SECONDS
            if ltp_ts is None:
                return (
                    self._uniform_predictions(STATE_UNAVAILABLE, "LTP timestamp is invalid; LTP freshness cannot be verified."),
                    ltp, source_ts, None,
                )
            ltp_age = (started - ltp_ts).total_seconds()
            if ltp_age < -self.MAX_CLOCK_SKEW_SECONDS or ltp_age > max_age:
                reason = f"Live LTP is stale (age={ltp_age:.1f}s, max={max_age:.0f}s)."
                return self._uniform_predictions(STATE_UNAVAILABLE, reason), ltp, source_ts, reason

        predictions: Dict[str, SingleStrategyPrediction] = {}
        for key, cls in STRATEGY_CLASSES.items():
            try:
                result = self._evaluate_strategy(key, cls, clean_symbol, prep, ltp)
                if not isinstance(result, SingleStrategyPrediction):
                    raise TypeError(f"evaluator returned {type(result).__name__}")
                predictions[key] = result
            except Exception as exc:  # strategy exception => ERROR, never a signal
                logger.error(f"[PredictionService] {cls.display_name} failed for {clean_symbol}: {type(exc).__name__}: {exc}")
                predictions[key] = self._error_prediction(cls.display_name, exc)

        return predictions, ltp, source_ts, None

    # ======================================================================
    # FRESHNESS
    # ======================================================================

    @staticmethod
    def _freshness_block(
        status: str,
        reasons: List[str],
        evaluated_at: Optional[datetime],
        source_candle_ts: Optional[datetime],
        started: Optional[datetime] = None,
        requested_at: Optional[datetime] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        ref = now or evaluated_at
        return {
            "status": status,
            "reason": "; ".join(reasons) if reasons else None,
            "evaluated_at": _iso(evaluated_at),
            "source_candle_ts": _iso(source_candle_ts),
            "source_candle_close_ts": _iso(
                source_candle_ts + timedelta(minutes=DEFAULT_TIMEFRAME_MINUTES)
            ) if source_candle_ts is not None else None,
            "candle_age_seconds": round(
                (ref - (source_candle_ts + timedelta(minutes=DEFAULT_TIMEFRAME_MINUTES))).total_seconds(), 1
            ) if (source_candle_ts is not None and ref is not None) else None,
            "evaluation_duration_ms": round((evaluated_at - started).total_seconds() * 1000.0)
            if (started is not None and evaluated_at is not None) else None,
            "request_to_result_seconds": round((evaluated_at - requested_at).total_seconds(), 2)
            if (requested_at is not None and evaluated_at is not None) else None,
        }

    @staticmethod
    def _mark_stale(prediction: SingleStrategyPrediction, reason: str) -> SingleStrategyPrediction:
        """
        Mark a result STALE. A stale directional signal is WITHHELD (converted to
        UNAVAILABLE) so an old signal can never be shown/voted as a fresh one.
        """
        freshness = dict(prediction.freshness or {})
        freshness["status"] = FRESHNESS_STALE
        existing = freshness.get("reason")
        freshness["reason"] = f"{existing}; {reason}" if existing and reason not in str(existing) else (existing or reason)

        if prediction.state != STATE_SIGNAL:
            return replace(prediction, freshness=freshness)

        metrics = dict(prediction.metrics or {})
        metrics["stale_prior_signal"] = {
            "status": prediction.status,
            "direction": prediction.direction,
            "signal_timestamp": _iso(prediction.signal_timestamp),
        }
        return replace(
            prediction,
            status=STATE_UNAVAILABLE,
            direction=None, entry=None, stop_loss=None, target=None,
            hedge_symbol=None, hedge_action=None, hedge_entry=None, hedge_legs=None,
            pair_prices=None, hedge_notional_weights=None,
            detail_status=None,
            reason=f"Stale result withheld: {reason}",
            metrics=metrics,
            freshness=freshness,
        )

    def _finalize(
        self,
        clean_symbol: str,
        predictions: Dict[str, SingleStrategyPrediction],
        ltp: Optional[float],
        source_ts: Optional[datetime],
        started: datetime,
        stale_reason: Optional[str],
        requested_at: Optional[Any],
    ) -> Tuple[Dict[str, SingleStrategyPrediction], Dict[str, Any]]:
        # Expire directional signals the live price has already breached.
        checked: Dict[str, SingleStrategyPrediction] = {}
        for key, prediction in predictions.items():
            try:
                checked[key] = self._invalidate_price_breached_signal(prediction, ltp)  # type: ignore[arg-type]
            except Exception as exc:
                checked[key] = self._error_prediction(key, exc)
        predictions = checked

        finished = now_ist_naive()
        req_ts = _normalize_ist_naive(requested_at) if requested_at is not None else None

        reasons: List[str] = []
        if stale_reason:
            reasons.append(stale_reason)
        if req_ts is not None:
            delay = (finished - req_ts).total_seconds()
            if delay > self.MAX_REQUEST_TO_RESULT_SECONDS:
                reasons.append(
                    f"evaluation delayed {delay:.1f}s after inputs were captured (max {self.MAX_REQUEST_TO_RESULT_SECONDS:.0f}s)"
                )
        duration = (finished - started).total_seconds()
        if duration > self.MAX_EVALUATION_DURATION_SECONDS:
            reasons.append(
                f"evaluation took {duration:.1f}s (max {self.MAX_EVALUATION_DURATION_SECONDS:.0f}s); LTP/book are older than the result"
            )
        if source_ts is not None:
            expected_now = self._expected_latest_candle_start(finished)
            if expected_now is not None and expected_now > source_ts:
                reasons.append(
                    f"a newer candle ({expected_now:%H:%M}) completed during evaluation; source candle is {source_ts:%H:%M}"
                )

        if source_ts is None:
            status = FRESHNESS_UNKNOWN if not reasons else FRESHNESS_STALE
        else:
            status = FRESHNESS_STALE if reasons else FRESHNESS_FRESH
        freshness = self._freshness_block(
            status, reasons, finished, source_ts, started=started, requested_at=req_ts
        )

        stamped: Dict[str, SingleStrategyPrediction] = {}
        for key, prediction in predictions.items():
            prediction.evaluated_at = finished
            prediction.source_candle_ts = source_ts
            prediction.freshness = dict(freshness)
            if prediction.strategy is None:
                prediction.strategy = key
            if prediction.symbol is None and clean_symbol:
                prediction.symbol = clean_symbol
            stamped[key] = self._mark_stale(prediction, "; ".join(reasons)) if reasons else prediction

        consensus = self.calculate_consensus(stamped)
        consensus["evaluated_at"] = _iso(finished)
        consensus["source_candle_ts"] = _iso(source_ts)
        consensus["freshness"] = dict(freshness)
        return stamped, consensus

    def revalidate_cached(
        self,
        predictions: Dict[str, SingleStrategyPrediction],
        now: Optional[datetime] = None,
        max_age_seconds: Optional[float] = None,
    ) -> Tuple[Dict[str, SingleStrategyPrediction], Dict[str, Any]]:
        """
        Re-assess freshness of previously computed results at SERVE time.

        MarketStreamManager must call this before re-serving any cached
        evaluation. A result is stale if it is older than ``max_age_seconds``
        (default MAX_RESULT_AGE_SECONDS), if a newer candle has completed since
        its source candle, or if it carries no evaluation timestamp (freshness
        cannot be proven). Stale directional signals are withheld.
        Cached objects are not mutated; new objects are returned.
        """
        now_ts = _normalize_ist_naive(now) if now is not None else now_ist_naive()
        limit = float(max_age_seconds) if max_age_seconds is not None else self.MAX_RESULT_AGE_SECONDS

        evaluated_at = next((p.evaluated_at for p in predictions.values() if p.evaluated_at is not None), None)
        source_ts = next((p.source_candle_ts for p in predictions.values() if p.source_candle_ts is not None), None)

        reasons: List[str] = []
        if evaluated_at is None:
            reasons.append("result has no evaluation timestamp; freshness cannot be verified")
        else:
            age = (now_ts - evaluated_at).total_seconds()
            if age > limit:
                reasons.append(f"result is {age:.1f}s old (max {limit:.0f}s)")
        if source_ts is not None:
            expected_now = self._expected_latest_candle_start(now_ts)
            if expected_now is not None and expected_now > source_ts:
                reasons.append(
                    f"a newer candle ({expected_now:%H:%M}) has completed; source candle is {source_ts:%H:%M}"
                )

        out: Dict[str, SingleStrategyPrediction] = {}
        reason_text = "; ".join(reasons)
        for key, prediction in predictions.items():
            if reasons:
                out[key] = self._mark_stale(prediction, reason_text)
            else:
                out[key] = replace(prediction)
        consensus = self.calculate_consensus(out)
        consensus["evaluated_at"] = _iso(evaluated_at)
        consensus["source_candle_ts"] = _iso(source_ts)
        consensus["freshness"] = self._freshness_block(
            FRESHNESS_STALE if reasons else (FRESHNESS_FRESH if source_ts is not None else FRESHNESS_UNKNOWN),
            reasons, evaluated_at, source_ts, now=now_ts,
        )
        return out, consensus

    # ======================================================================
    # SHARED EVALUATOR HELPERS
    # ======================================================================

    def _directional_prediction(
        self,
        signal: Optional[StrategySignal],
        ltp: float,
        long_status: str,
        short_status: str,
        long_reason: str,
        short_reason: str,
        levels: Dict[str, Any],
    ) -> Optional[SingleStrategyPrediction]:
        if signal is None:
            return None
        if signal.action == SignalAction.BUY:
            return self._prediction_from_signal(long_status, signal, ltp, long_reason, levels)
        if signal.action == SignalAction.SELL:
            return self._prediction_from_signal(short_status, signal, ltp, short_reason, levels)
        return None


    # ======================================================================
    # STRATEGY REPLAY (Gap Fade / RSI x EMA / ORB-SIP)
    # ======================================================================

    @staticmethod
    def _replay_strategy(
        strategy: IntradaySignalStrategy,
        prep: _PreparedCandles,
    ) -> _ReplayOutcome:
        """
        Seed the strategy with prior sessions, then feed today's completed
        candles in order. A signal is treated as taken at once, so the
        strategy itself reports the stop / target / 15:15 exit afterwards and
        an old signal is never shown again once it has resolved.
        """
        strategy.seed_context(prep.lookback_df.copy())
        strategy.reset_session(prep.latest_date)
        current: Optional[StrategySignal] = None
        for _, row in prep.today_df.iterrows():
            sig = strategy.on_candle(PredictionService._candle_dict(row), float(row["vwap"]))
            if sig is None:
                continue
            if sig.action in (SignalAction.BUY, SignalAction.SELL):
                current = sig
                strategy.register_trade_entry(
                    entry_price=float(sig.price),
                    position=1 if sig.action == SignalAction.BUY else -1,
                    stop_loss=float(sig.stop_loss),
                    target=float(sig.target),
                    risk_dist=abs(float(sig.price) - float(sig.stop_loss)),
                )
            elif sig.action == SignalAction.EXIT:
                current = None
                strategy.register_trade_exit()
        flags = {
            "signalled_today": bool(strategy.signalled_today),
            "exit_reason": strategy.last_exit_reason,
            "note": getattr(strategy, "status_note", ""),
            "context_ready": strategy.daily_atr is not None and strategy.prev_close is not None,
        }
        return _ReplayOutcome(current, strategy.levels(), flags)

    def _evaluate_strategy(
        self,
        key: str,
        cls: Any,
        symbol: str,
        data: Any,
        ltp: float,
    ) -> SingleStrategyPrediction:
        prep, failure = self._resolve_prepared(symbol, data)
        if failure is not None:
            return failure
        warm = self._warmup_failure(prep, cls.display_name, full_history=True)
        if warm is not None:
            return warm

        inst = InstrumentConfig(symbol=symbol)
        outcome = self._cached_replay(key, inst, prep, lambda: self._replay_strategy(cls(symbol), prep))
        levels = outcome.levels

        if outcome.signal is not None:
            label = key.upper()
            directional = self._directional_prediction(
                outcome.signal, ltp,
                f"{label}_LONG", f"{label}_SHORT",
                f"{cls.display_name} buy signal.", f"{cls.display_name} sell signal.",
                levels,
            )
            if directional is not None:
                return directional

        if not outcome.flags["context_ready"]:
            return SingleStrategyPrediction(
                STATE_WAITING,
                reason="Needs 10+ prior sessions of 15-minute candles (daily range / volume history).",
                levels=levels,
            )
        exit_reason = outcome.flags["exit_reason"]
        if outcome.flags["signalled_today"] and exit_reason:
            return SingleStrategyPrediction(
                STATE_NO_TRADE,
                reason=f"Today's signal is closed ({exit_reason.replace('_', ' ').lower()}). One signal per day.",
                levels=levels,
            )
        note = outcome.flags["note"] or "No setup on the latest completed candle."
        state = STATE_WAITING if not outcome.flags["signalled_today"] and "Waiting" in note else STATE_NO_TRADE
        return SingleStrategyPrediction(state, reason=note, levels=levels)


    # ======================================================================
    # SIGNAL EXPIRATION
    # ======================================================================

    @staticmethod
    def _no_trade_from(prediction: SingleStrategyPrediction, reason: str) -> SingleStrategyPrediction:
        return SingleStrategyPrediction(
            status=STATE_NO_TRADE,
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
            signal_timestamp=prediction.signal_timestamp,
        )

    @staticmethod
    def _invalidate_price_breached_signal(
        prediction: SingleStrategyPrediction,
        current_ltp: Optional[float],
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

    @staticmethod
    def _field(p: Any, name: str) -> Any:
        return p.get(name) if isinstance(p, dict) else getattr(p, name, None)

    @classmethod
    def _state_of(cls, p: Any) -> str:
        """Canonical state of a prediction object or serialised dict (re-derived, fail closed)."""
        derived = classify_strategy_state(
            cls._field(p, "status"),
            cls._field(p, "direction"),
            cls._field(p, "entry"),
            cls._field(p, "stop_loss"),
            cls._field(p, "target"),
        )
        declared = str(cls._field(p, "state") or "").strip().upper()
        if declared in STRATEGY_STATES and declared != derived:
            # Disagreement between a declared state and the data: never trust the more
            # permissive one - a vote requires BOTH to say SIGNAL.
            return derived if STATE_SIGNAL not in (declared, derived) else STATE_ERROR
        return derived

    @classmethod
    def _is_stale(cls, p: Any) -> bool:
        freshness = cls._field(p, "freshness")
        return isinstance(freshness, dict) and str(freshness.get("status") or "").upper() == FRESHNESS_STALE

    @classmethod
    def calculate_consensus(
        cls,
        predictions: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Equal-vote consensus over live strategies."""
        predictions = predictions or {}
        consensus_keys = [k for k in cls.LIVE_CONSENSUS_STRATEGIES if k in predictions]
        total_live = len(consensus_keys)

        normalized_predictions = {name: predictions[name] for name in consensus_keys}
        excluded_strategies = sorted(
            name for name in predictions if name not in cls.LIVE_CONSENSUS_STRATEGIES
        )

        counts = {
            "buy_votes": 0,
            "sell_votes": 0,
            "waiting": 0,
            "no_trade": 0,
            "unavailable": 0,
            "not_applicable": 0,
            "errors": 0,
        }
        missing: List[str] = [k for k in cls.LIVE_CONSENSUS_STRATEGIES if k not in predictions]
        stale_withheld: List[str] = []

        def _get_status(p: Any) -> str:
            if isinstance(p, dict):
                return str(p.get("status") or "")
            return str(getattr(p, "status", "") or "")

        def _get_direction(p: Any) -> Optional[str]:
            if isinstance(p, dict):
                return p.get("direction")
            return getattr(p, "direction", None)

        def _is_stale_pred(p: Any) -> bool:
            freshness = cls._field(p, "freshness")
            return isinstance(freshness, dict) and str(freshness.get("status") or "").upper() == FRESHNESS_STALE

        for key, p in normalized_predictions.items():
            status = _get_status(p).upper()
            state = cls._state_of(p)
            d = _direction_bucket(_get_direction(p))
            if _is_stale_pred(p) and state == STATE_SIGNAL:
                stale_withheld.append(key)
                counts["unavailable"] += 1
            elif status in {"UNAVAILABLE"} or state == STATE_UNAVAILABLE:
                counts["unavailable"] += 1
            elif status in {"ERROR"} or state == STATE_ERROR:
                counts["errors"] += 1
            elif status in {"NOT_APPLICABLE"} or state == STATE_NOT_APPLICABLE:
                counts["not_applicable"] += 1
            elif state == STATE_WAITING or status == "WAITING":
                counts["waiting"] += 1
            elif state == STATE_NO_TRADE or status == "NO_TRADE":
                counts["no_trade"] += 1
            elif d == "BUY":
                counts["buy_votes"] += 1
            elif d == "SELL":
                counts["sell_votes"] += 1
            else:
                counts["no_trade"] += 1

        long_count = counts["buy_votes"]
        short_count = counts["sell_votes"]
        directional_count = long_count + short_count

        evaluable_count = sum(
            1
            for k, p in normalized_predictions.items()
            if _get_status(p).upper() not in {"UNAVAILABLE", "ERROR", "NOT_APPLICABLE", ""}
            and cls._state_of(p) not in {STATE_UNAVAILABLE, STATE_ERROR, STATE_NOT_APPLICABLE}
            and not (_is_stale_pred(p) and cls._state_of(p) == STATE_SIGNAL)
        )

        base = {
            "total_strategies": total_live,
            "applicable_strategies": total_live - counts["not_applicable"],
            "evaluable_strategies": evaluable_count,
            "directional_strategies": directional_count,
            "directional_evaluable": directional_count,
            **counts,
            "participation_pct": round(directional_count / evaluable_count * 100.0, 1) if evaluable_count > 0 else None,
            "consensus_strategies": list(cls.LIVE_CONSENSUS_STRATEGIES),
            "excluded_strategies": excluded_strategies,
            "missing_strategies": missing,
            "stale_withheld_strategies": stale_withheld,
        }

        if total_live == 0 or evaluable_count == 0:
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
            label = f"{strength} LONG ({long_count}/{evaluable_count})"
        else:
            direction = "SHORT"
            agreeing = short_count
            strength = "STRONG" if short_count >= 3 else "MODERATE" if short_count >= 2 else "WEAK"
            label = f"{strength} SHORT ({short_count}/{evaluable_count})"

        agreement_pct = round((agreeing / evaluable_count) * 100.0, 1) if evaluable_count > 0 else None
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
        # A stale candidate must never be surfaced as a current top pick.
        candidates = [
            c for c in (candidates or [])
            if str((c.freshness or (c.consensus or {}).get("freshness") or {}).get("status", "")).upper()
            != FRESHNESS_STALE
        ]
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