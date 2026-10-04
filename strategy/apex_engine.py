"""
APEX-AIVEM intraday strategy.

This module is strategy-only: it consumes candles and externally supplied
market context and emits StrategySignal objects. It does not connect to a
broker or place/cancel orders.

Key correctness rules
---------------------
1. Incoming intraday timestamps are treated as candle OPEN timestamps by
   default. Signal/exit timestamps are converted to candle COMPLETION time.
2. The opening price used for the APEX gap must come from the first session
   candle at market_open. A late/missing opening candle cannot fabricate a
   pre-market gap.
3. Gap/ATR uses prior-session history only. The current candle cannot change
   the denominator of the already-known opening gap.
4. RVOL uses the same intraday time-slot from prior sessions when possible,
   avoiding the strong U-shaped intraday volume bias.
5. Missing volume does NOT pass the RVOL gate. It is treated as unavailable.
6. The composite score is directionally consistent: signed inputs stay
   signed; magnitude-only volume is aligned with the measured gap direction.
7. Catalyst scoring is signed when inferred from headlines. Ambiguous
   headlines remain neutral rather than being treated as bullish/bearish.
8. cpr_norm is accepted only for backward compatibility and is NOT used in
   the APEX score. APEX remains independent of CPR/EMA/ORB outputs.
9. Existing positions are managed throughout the session, even after the
   entry window closes.
10. Initial stop/target levels must be structurally valid and respect the
    instrument stop-distance cap.
11. Gap-through stop/target exits are filled at candle OPEN in backtests.
12. Same-candle stop/target collisions use the selected execution policy.
13. Breakeven is activated at the configured R multiple.
14. The strategy preserves the BaseStrategy fill-confirmation contract. The
    backtest/execution layer should call register_trade_entry/exit after
    accepted fills/exits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from config.settings import InstrumentConfig, InstrumentType, StrategyConfig, settings
from monitoring.logger import logger
from strategy.base_strategy import BaseStrategy, SignalAction, StrategySignal


# =====================================================================
# CONFIGURATION
# =====================================================================


@dataclass
class EngineConfig:
    # Opening-gap gates, expressed in ATR units of the prior session.
    gap_atr_min: float = 0.60
    gap_atr_max: float = 2.50

    # Volume gate.
    rvol_min: float = 1.80

    # Composite weights. They are normalized across inputs that are actually
    # available; missing sector/market/news inputs do not become fake zeros.
    w_gap: float = 0.24
    w_vol: float = 0.19
    w_oir: float = 0.14
    w_sector: float = 0.19
    w_market: float = 0.10
    w_cat: float = 0.14

    # Minimum absolute composite score required after confirmation.
    min_composite_score: float = 0.30

    # Candle confirmation.
    max_wick_ratio: float = 0.35

    # APEX-specific trade levels.
    stop_atr_mult: float = 0.85
    target_atr_mult: Optional[float] = 1.50

    # Regime.
    vix_halt_below: float = 11.50
    vix_full_deploy_max: float = 22.00
    vix_high_risk_mult: float = 0.50

    # Timing, IST.
    entry_start: time = time(9, 30)
    entry_end: time = time(13, 30)

    # History.
    atr_period: int = 14
    volume_lookback: int = 20

    # Data assumptions.
    candle_timestamps_are_open: bool = True
    market_open: time = time(9, 15)

    # Optional alternative to target_atr_mult. When target_atr_mult is None,
    # the application's global risk_reward_ratio is used instead.


# =====================================================================
# UTILS
# =====================================================================


def _normalize_ist_naive(value) -> datetime:
    """Normalize a timestamp to Asia/Kolkata and return a naive datetime."""
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
    return ts.to_pydatetime()


def _safe_float(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def _clip_signed(value: float, limit: float = 1.0) -> float:
    return float(np.clip(float(value), -limit, limit))


# =====================================================================
# CATALYST
# =====================================================================


class CatalystScorer:
    """
    Deterministic signed catalyst classifier.

    Returns approximately -3..+3:
        +3 = major positive catalyst
        +2 = material positive catalyst
        +1 = minor positive catalyst
         0 = neutral / ambiguous / no catalyst
        -1 = minor negative catalyst
        -2 = material negative catalyst
        -3 = major negative catalyst

    A custom LLM callback may be supplied, but its contract is explicitly
    signed: it must return a value in [-3, 3]. An unsigned magnitude is not
    silently interpreted as bullish.
    """

    _POS_MAJOR = (
        "index inclusion",
    )

    _NEG_MAJOR = (
        "fraud",
        "insolvency",
        "regulatory action",
        "index exclusion",
        "trading ban",
        "sebi action",
    )

    _POS_MATERIAL = (
        "order win",
        "contract win",
        "rating upgrade",
        "buyback",
        "capacity expansion",
        "dividend",
    )

    _NEG_MATERIAL = (
        "rating downgrade",
        "credit downgrade",
        "default",
        "order cancellation",
        "contract cancellation",
        "guidance cut",
    )

    _POS_MINOR = (
        "board approves",
        "board approval",
        "funding secured",
        "partnership",
    )

    _NEG_MINOR = (
        "board resignation",
        "warning letter",
        "show cause",
        "investigation",
    )

    # Deliberately ambiguous: merger/acquisition/results/etc. do not carry a
    # reliable direction by themselves and therefore contribute zero.

    def __init__(self, llm_classify_fn: Optional[Callable] = None):
        self.llm_classify_fn = llm_classify_fn

    @staticmethod
    def _validate_score(value) -> Optional[int]:
        try:
            score = int(value)
        except (TypeError, ValueError):
            return None
        if score < -3 or score > 3:
            return None
        return score

    def score(self, symbol: str, headlines: List[str]) -> int:
        if not headlines:
            return 0

        if self.llm_classify_fn is not None:
            try:
                value = self.llm_classify_fn(symbol, headlines)
            except TypeError:
                try:
                    result = self.llm_classify_fn({symbol: headlines})
                    if isinstance(result, dict):
                        value = result.get(symbol, 0)
                    else:
                        value = result
                except Exception as exc:
                    logger.warning(f"[{symbol}] APEX LLM catalyst scorer failed: {exc}")
                    value = None
            except Exception as exc:
                logger.warning(f"[{symbol}] APEX LLM catalyst scorer failed: {exc}")
                value = None

            validated = self._validate_score(value)
            if validated is not None:
                return validated

        text = " | ".join(str(h).lower() for h in headlines)

        if any(k in text for k in self._NEG_MAJOR):
            return -3
        if any(k in text for k in self._POS_MAJOR):
            return 3
        if any(k in text for k in self._NEG_MATERIAL):
            return -2
        if any(k in text for k in self._POS_MATERIAL):
            return 2
        if any(k in text for k in self._NEG_MINOR):
            return -1
        if any(k in text for k in self._POS_MINOR):
            return 1

        return 0


# =====================================================================
# REGIME
# =====================================================================


@dataclass
class RegimeDecision:
    trading_enabled: bool
    risk_multiplier: float
    reason: str


def evaluate_regime(
    cfg: EngineConfig,
    india_vix: Optional[float],
    calendar_blackout: bool = False,
) -> RegimeDecision:
    """
    Apply APEX regime gates.

    VIX is a trade-enable/diagnostic input here. The returned risk_multiplier
    must be consumed by the outer risk/execution layer if it is to alter
    monetary risk. A StrategySignal has no position-size field, so the
    strategy does not pretend that the multiplier changes quantity itself.
    """
    if calendar_blackout:
        return RegimeDecision(
            trading_enabled=False,
            risk_multiplier=0.0,
            reason="Calendar blackout",
        )

    if india_vix is None:
        return RegimeDecision(
            trading_enabled=True,
            risk_multiplier=1.0,
            reason="India VIX unavailable; normal APEX evaluation",
        )

    vix = _safe_float(india_vix)
    if vix is None or vix < 0:
        return RegimeDecision(
            trading_enabled=True,
            risk_multiplier=1.0,
            reason="Invalid India VIX input; normal APEX evaluation",
        )

    if vix < cfg.vix_halt_below:
        return RegimeDecision(
            trading_enabled=False,
            risk_multiplier=0.0,
            reason=f"India VIX {vix:.2f} below halt threshold",
        )

    if vix <= cfg.vix_full_deploy_max:
        return RegimeDecision(
            trading_enabled=True,
            risk_multiplier=1.0,
            reason=f"India VIX {vix:.2f}: normal regime",
        )

    return RegimeDecision(
        trading_enabled=True,
        risk_multiplier=cfg.vix_high_risk_mult,
        reason=f"India VIX {vix:.2f}: high-volatility regime",
    )


# =====================================================================
# APEX STRATEGY
# =====================================================================


class ApexAivemStrategy(BaseStrategy):
    """
    APEX-AIVEM independent intraday strategy.

    External context supported:
        India VIX
        calendar blackout
        GIFT Nifty gap
        sector relative strength
        market relative strength
        signed catalyst score / timestamp-safe headlines
        order imbalance

    CPR/EMA/ORB outputs are intentionally NOT consumed in the signal score.
    `cpr_norm` is accepted by set_context only for backwards compatibility
    and is recorded as ignored input.
    """

    def __init__(
        self,
        instrument: InstrumentConfig,
        strategy_config: StrategyConfig = settings.strategy,
        config: Optional[EngineConfig] = None,
        catalyst_scorer: Optional[CatalystScorer] = None,
    ):
        super().__init__(instrument.symbol)

        self.instrument = instrument
        self.config = strategy_config
        self.cfg = config or EngineConfig()

        self._validate_config()

        self.catalyst_scorer = catalyst_scorer or CatalystScorer()

        self.current_date: Optional[date] = None

        self.warm_history = pd.DataFrame(
            columns=["datetime", "open", "high", "low", "close", "volume"]
        )
        self.today_bars: List[dict] = []

        self.previous_close: Optional[float] = None
        self.previous_atr: Optional[float] = None
        self.today_open: Optional[float] = None
        self._opening_candle_seen = False

        self.context: Dict[str, Any] = {}
        self.regime: Optional[RegimeDecision] = None
        self.last_analysis: Dict[str, Any] = {}

    # -----------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------

    def _validate_config(self) -> None:
        if self.cfg.gap_atr_min < 0:
            raise ValueError("gap_atr_min must be >= 0")
        if self.cfg.gap_atr_max <= self.cfg.gap_atr_min:
            raise ValueError("gap_atr_max must be > gap_atr_min")
        if self.cfg.rvol_min <= 0:
            raise ValueError("rvol_min must be > 0")
        if self.cfg.min_composite_score < 0:
            raise ValueError("min_composite_score must be >= 0")
        if self.cfg.stop_atr_mult <= 0:
            raise ValueError("stop_atr_mult must be > 0")
        if self.cfg.target_atr_mult is not None and self.cfg.target_atr_mult <= 0:
            raise ValueError("target_atr_mult must be > 0 when supplied")
        if self.cfg.atr_period < 2:
            raise ValueError("atr_period must be >= 2")
        if self.cfg.volume_lookback < 2:
            raise ValueError("volume_lookback must be >= 2")

        weights = [
            self.cfg.w_gap,
            self.cfg.w_vol,
            self.cfg.w_oir,
            self.cfg.w_sector,
            self.cfg.w_market,
            self.cfg.w_cat,
        ]
        if any(w < 0 for w in weights):
            raise ValueError("APEX weights must be >= 0")
        if sum(weights) <= 0:
            raise ValueError("At least one APEX weight must be > 0")

    # -----------------------------------------------------------------
    # Context
    # -----------------------------------------------------------------

    def set_context(
        self,
        *,
        india_vix: Optional[float] = None,
        calendar_blackout: bool = False,
        gift_nifty_gap: Optional[float] = None,
        sector_rs: Optional[float] = None,
        market_rs: Optional[float] = None,
        catalyst_score: Optional[int] = None,
        cpr_norm: Optional[float] = None,
        order_imbalance: Optional[float] = None,
        headlines: Optional[List[str]] = None,
    ) -> None:
        """
        Store timestamp-safe external context.

        Important:
            cpr_norm is ignored intentionally. It is accepted only so older
            callers do not break, but APEX remains independent of CPR output.

        For headlines, the deterministic scorer is used unless a valid signed
        catalyst_score is explicitly supplied.
        """
        if catalyst_score is None and headlines is not None:
            catalyst_score = self.catalyst_scorer.score(
                self.symbol,
                headlines,
            )

        validated_catalyst = CatalystScorer._validate_score(catalyst_score)
        validated_oir = _safe_float(order_imbalance)
        if validated_oir is not None:
            validated_oir = _clip_signed(validated_oir)

        self.context = {
            "india_vix": _safe_float(india_vix),
            "calendar_blackout": bool(calendar_blackout),
            "gift_nifty_gap": _safe_float(gift_nifty_gap),
            "sector_rs": _clip_signed(sector_rs) if _safe_float(sector_rs) is not None else None,
            "market_rs": _clip_signed(market_rs) if _safe_float(market_rs) is not None else None,
            "catalyst_score": validated_catalyst,
            # Backwards-compatible diagnostic only. Never fed into score.
            "cpr_norm_ignored": _safe_float(cpr_norm),
            "order_imbalance": validated_oir,
        }

        self.regime = evaluate_regime(
            self.cfg,
            self.context["india_vix"],
            calendar_blackout=self.context["calendar_blackout"],
        )

    # -----------------------------------------------------------------
    # Context seeding
    # -----------------------------------------------------------------

    @staticmethod
    def _wilder_atr(df: pd.DataFrame, period: int) -> pd.Series:
        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        return tr.ewm(
            alpha=1.0 / period,
            adjust=False,
            min_periods=period,
        ).mean()

    def seed_context(self, historical_bars: pd.DataFrame) -> None:
        """
        Store prior-session history and precompute prior ATR/close.

        Caller contract: historical_bars contains only data strictly before
        today's session. We never append today's bars here.
        """
        columns = ["datetime", "open", "high", "low", "close", "volume"]

        if historical_bars is None or historical_bars.empty:
            self.warm_history = pd.DataFrame(columns=columns)
            self.previous_close = None
            self.previous_atr = None
            return

        data = historical_bars.copy()

        if "datetime" not in data.columns:
            if isinstance(data.index, pd.DatetimeIndex):
                data["datetime"] = data.index
            else:
                raise ValueError("APEX requires datetime column/index")

        required = ["datetime", "open", "high", "low", "close"]
        missing = [c for c in required if c not in data.columns]
        if missing:
            raise ValueError(f"APEX missing required columns: {missing}")

        if "volume" not in data.columns:
            data["volume"] = np.nan

        data = data[columns].copy()
        data["datetime"] = data["datetime"].map(_normalize_ist_naive)

        for col in ["open", "high", "low", "close", "volume"]:
            data[col] = pd.to_numeric(data[col], errors="coerce")

        data = data.dropna(subset=["datetime", "open", "high", "low", "close"])
        data = data[(data["high"] >= data["low"]) & (data["high"] >= 0) & (data["low"] >= 0)]
        data = data.sort_values("datetime")
        data = data.drop_duplicates("datetime", keep="last")

        self.warm_history = data.reset_index(drop=True)

        if self.warm_history.empty:
            self.previous_close = None
            self.previous_atr = None
            return

        self.previous_close = float(self.warm_history["close"].iloc[-1])

        atr_series = self._wilder_atr(
            self.warm_history,
            self.cfg.atr_period,
        )
        valid_atr = atr_series.dropna()
        self.previous_atr = float(valid_atr.iloc[-1]) if not valid_atr.empty else None

    # -----------------------------------------------------------------
    # Session
    # -----------------------------------------------------------------

    def reset_session(self, session_date: date):
        self.current_date = session_date

        # Per-session position state.
        self.position = 0
        self.entry_price = 0.0
        self.stop_loss = 0.0
        self.target = 0.0
        self.initial_risk_dist = 0.0
        self.trailing_breakeven_active = False
        self.trades_today = 0

        self.today_bars = []
        self.today_open = None
        self._opening_candle_seen = False
        self.last_analysis = {}

    # -----------------------------------------------------------------
    # Data helpers
    # -----------------------------------------------------------------

    def _event_timestamp(self, raw_timestamp) -> datetime:
        ts = _normalize_ist_naive(raw_timestamp)

        if self.cfg.candle_timestamps_are_open:
            minutes = int(
                getattr(
                    self.config,
                    "candle_timeframe_minutes",
                    15,
                )
            )
            if minutes <= 0:
                raise ValueError("candle_timeframe_minutes must be > 0")
            ts += pd.Timedelta(minutes=minutes).to_pytimedelta()

        return ts

    def _combined_history(self) -> pd.DataFrame:
        today = pd.DataFrame(self.today_bars)

        if self.warm_history.empty:
            combined = today
        elif today.empty:
            combined = self.warm_history.copy()
        else:
            combined = pd.concat(
                [self.warm_history, today],
                ignore_index=True,
            )

        if combined.empty:
            return pd.DataFrame(
                columns=["datetime", "open", "high", "low", "close", "volume"]
            )

        combined["datetime"] = combined["datetime"].map(_normalize_ist_naive)
        combined = combined.sort_values("datetime")
        combined = combined.drop_duplicates("datetime", keep="last")
        return combined.reset_index(drop=True)

    # -----------------------------------------------------------------
    # Features
    # -----------------------------------------------------------------

    def _calculate_features(
        self,
        candle: dict,
        vwap: Optional[float],
    ) -> Dict[str, Any]:
        data = self._combined_history()
        if data.empty:
            return {}

        indicators = data.copy()
        indicators["atr14"] = self._wilder_atr(
            indicators,
            self.cfg.atr_period,
        )

        latest = indicators.iloc[-1]
        atr = _safe_float(latest["atr14"])
        close = _safe_float(candle.get("close"))
        high = _safe_float(candle.get("high"))
        low = _safe_float(candle.get("low"))
        open_price = _safe_float(candle.get("open"))
        volume = _safe_float(candle.get("volume"))

        if any(v is None for v in [atr, close, high, low, open_price]):
            return {}
        if atr <= 0 or high < low:
            return {}

        # Gap MUST use the already-known opening price and PRIOR ATR.
        if self.today_open is None or self.previous_close is None or self.previous_atr is None:
            gap_atr = None
        else:
            gap_atr = (
                (self.today_open - self.previous_close)
                / self.previous_atr
            )

        # Same-slot RVOL: compare the current candle's volume with prior
        # sessions at the same clock time. This avoids mixing 09:15 volume
        # with 13:15 volume.
        current_raw_dt = _normalize_ist_naive(candle["datetime"])
        slot = current_raw_dt.time()

        prior = self.warm_history.copy()
        rvol = None
        avg_volume = None

        if not prior.empty and volume is not None and volume > 0:
            prior["slot"] = prior["datetime"].map(lambda x: x.time())
            same_slot = prior[prior["slot"] == slot]
            same_slot = same_slot.dropna(subset=["volume"])
            same_slot = same_slot[same_slot["volume"] > 0]

            if not same_slot.empty:
                # Approximate a prior-session lookback in calendar order by
                # selecting the latest N observations of this time slot.
                same_slot = same_slot.tail(self.cfg.volume_lookback)
                avg_volume = _safe_float(same_slot["volume"].mean())
                if avg_volume and avg_volume > 0:
                    rvol = volume / avg_volume

        supplied_oir = self.context.get("order_imbalance")
        if supplied_oir is not None:
            oir = _clip_signed(float(supplied_oir))
            oir_source = "BOOK"
        else:
            oir = None
            oir_source = "UNAVAILABLE"

        # Feature scores are normalized to roughly [-1, +1].
        if gap_atr is not None:
            gap_component = _clip_signed(
                gap_atr / max(self.cfg.gap_atr_max, 1e-9)
            )
        else:
            gap_component = None

        volume_component = None
        if rvol is not None and gap_atr is not None:
            # RVOL is magnitude, not direction. Align it with the measured
            # gap direction; RVOL cannot reverse the gap direction by itself.
            gap_sign = 1.0 if gap_atr >= 0 else -1.0
            volume_component = gap_sign * float(
                np.clip(
                    (np.log(max(rvol, 1e-9)) / np.log(4.0)),
                    0.0,
                    1.0,
                )
            )

        sector_rs = self.context.get("sector_rs")
        market_rs = self.context.get("market_rs")
        catalyst_score = self.context.get("catalyst_score")

        active_components: List[tuple[float, float]] = []

        if gap_component is not None:
            active_components.append((self.cfg.w_gap, gap_component))

        if volume_component is not None:
            active_components.append((self.cfg.w_vol, volume_component))

        if oir is not None:
            active_components.append(
                (self.cfg.w_oir, oir)
            )

        if sector_rs is not None:
            active_components.append((self.cfg.w_sector, float(sector_rs)))

        if market_rs is not None:
            active_components.append((self.cfg.w_market, float(market_rs)))

        if catalyst_score is not None and catalyst_score != 0:
            active_components.append(
                (
                    self.cfg.w_cat,
                    float(np.clip(catalyst_score / 3.0, -1.0, 1.0)),
                )
            )

        total_weight = sum(weight for weight, _ in active_components)
        if total_weight > 0:
            composite = sum(weight * component for weight, component in active_components) / total_weight
        else:
            composite = 0.0

        vwap_distance_pct = None
        vwap_value = _safe_float(vwap)
        if vwap_value is not None and vwap_value > 0:
            vwap_distance_pct = ((close - vwap_value) / vwap_value) * 100.0

        return {
            "atr": float(atr),
            "previous_atr": self.previous_atr,
            "gap_atr": gap_atr,
            "rvol": rvol,
            "avg_slot_volume": avg_volume,
            "oir": oir,
            "oir_source": oir_source,
            "sector_rs": sector_rs,
            "market_rs": market_rs,
            "catalyst_score": catalyst_score,
            "cpr_norm": self.context.get("cpr_norm_ignored"),
            "cpr_norm_used": False,
            "vwap_distance_pct": vwap_distance_pct,
            "apex_score": float(composite),
            "active_weight": float(total_weight),
            "close": float(close),
            "today_open": self.today_open,
            "previous_close": self.previous_close,
            "risk_multiplier": self.regime.risk_multiplier if self.regime else 1.0,
        }

    # Backwards-compatible helpers used by some callers/tests.
    def _weight_gap(self, value: float) -> float:
        return self.cfg.w_gap * float(value)

    def _weight_volume(self, value: float) -> float:
        return self.cfg.w_vol * float(value)

    # -----------------------------------------------------------------
    # Risk / levels
    # -----------------------------------------------------------------

    def _max_allowed_stop_distance(self, entry: float) -> float:
        entry = float(entry)
        tick = max(float(getattr(self.instrument, "tick_size", 0.05)), 1e-8)

        cap = _safe_float(getattr(self.instrument, "max_risk_cap", None))
        if cap is not None and cap > 0:
            return max(cap, tick)

        # Conservative equity fallback if an instrument configuration did
        # not supply a cap.
        if self.instrument.instrument_type == InstrumentType.EQUITY:
            pct = _safe_float(getattr(self.instrument, "equity_orb_max_risk_pct", None))
            if pct and pct > 0:
                return max(entry * pct, tick)

        return float("inf")

    def _round_to_tick(self, price: float) -> float:
        tick = float(getattr(self.instrument, "tick_size", 0.05))
        if tick <= 0:
            return float(price)
        return round(round(float(price) / tick) * tick, 10)

    def _build_levels(
        self,
        entry: float,
        atr: float,
        direction: str,
    ) -> Optional[tuple[float, float, float]]:
        raw_risk = self.cfg.stop_atr_mult * atr
        if raw_risk <= 0:
            return None

        if raw_risk > self._max_allowed_stop_distance(entry):
            return None

        stop = (
            entry - raw_risk
            if direction == "LONG"
            else entry + raw_risk
        )
        stop = self._round_to_tick(stop)
        risk_distance = abs(entry - stop)

        if risk_distance <= 0:
            return None

        if self.cfg.target_atr_mult is None:
            rr = _safe_float(getattr(self.config, "risk_reward_ratio", 2.0)) or 2.0
            if rr <= 0:
                return None
            target_distance = risk_distance * rr
        else:
            target_distance = self.cfg.target_atr_mult * atr

        target = (
            entry + target_distance
            if direction == "LONG"
            else entry - target_distance
        )
        target = self._round_to_tick(target)

        if direction == "LONG":
            valid = stop < entry < target
        else:
            valid = target < entry < stop

        if not valid:
            return None

        return float(stop), float(target), float(risk_distance)

    # -----------------------------------------------------------------
    # Entry generation
    # -----------------------------------------------------------------

    def _generate_entry_signal(
        self,
        candle: dict,
        features: Dict[str, Any],
    ) -> Optional[StrategySignal]:
        if self.position != 0:
            return None

        if self.trades_today >= int(
            getattr(self.config, "max_trades_per_instrument_day", 1)
        ):
            return None

        timestamp = _normalize_ist_naive(candle["datetime"])
        bar_time = timestamp.time()

        if not (self.cfg.entry_start <= bar_time <= self.cfg.entry_end):
            return None

        if self.regime is not None and not self.regime.trading_enabled:
            return None

        gap_atr = features.get("gap_atr")
        rvol = features.get("rvol")
        score = float(features.get("apex_score", 0.0))
        atr = float(features["atr"])

        # The gate must fail closed when these inputs are genuinely required.
        if gap_atr is None:
            self.last_analysis = {
                **features,
                "status": "NO_TRADE",
                "reason": "Opening gap unavailable: no verified session-open candle or prior ATR",
            }
            return None

        abs_gap = abs(float(gap_atr))
        if abs_gap < self.cfg.gap_atr_min:
            self.last_analysis = {
                **features,
                "status": "NO_TRADE",
                "reason": "Gap below APEX minimum threshold",
            }
            return None

        if abs_gap > self.cfg.gap_atr_max:
            self.last_analysis = {
                **features,
                "status": "NO_TRADE",
                "reason": "Gap above APEX maximum threshold",
            }
            return None

        if rvol is None:
            self.last_analysis = {
                **features,
                "status": "NO_TRADE",
                "reason": "Relative volume unavailable; RVOL gate fails closed",
            }
            return None

        if rvol < self.cfg.rvol_min:
            self.last_analysis = {
                **features,
                "status": "NO_TRADE",
                "reason": "Relative volume below APEX threshold",
            }
            return None

        if abs(score) < self.cfg.min_composite_score:
            self.last_analysis = {
                **features,
                "status": "WAITING",
                "reason": "APEX composite score below signal threshold",
            }
            return None

        open_price = float(candle["open"])
        close = float(candle["close"])
        high = float(candle["high"])
        low = float(candle["low"])
        tick = max(float(self.instrument.tick_size), 1e-8)

        candle_range = max(high - low, tick)
        upper_wick_ratio = max(0.0, (high - close) / candle_range)
        lower_wick_ratio = max(0.0, (close - low) / candle_range)

        vwap = _safe_float(self.last_analysis.get("vwap"))
        # vwap is supplied separately into on_candle, so this key is inserted
        # before calling this function.
        if vwap is None or vwap <= 0:
            self.last_analysis = {
                **features,
                "status": "NO_TRADE",
                "reason": "VWAP unavailable; APEX confirmation fails closed",
            }
            return None

        long_confirmation = (
            close > open_price
            and close > vwap
            and upper_wick_ratio <= self.cfg.max_wick_ratio
        )

        short_confirmation = (
            close < open_price
            and close < vwap
            and lower_wick_ratio <= self.cfg.max_wick_ratio
        )

        if score > 0 and long_confirmation:
            direction = "LONG"
            action = SignalAction.BUY
        elif score < 0 and short_confirmation:
            direction = "SHORT"
            action = SignalAction.SELL
        else:
            self.last_analysis = {
                **features,
                "status": "WAITING",
                "reason": "APEX directional score and candle/VWAP confirmation disagree",
                "upper_wick_ratio": upper_wick_ratio,
                "lower_wick_ratio": lower_wick_ratio,
            }
            return None

        levels = self._build_levels(
            entry=close,
            atr=atr,
            direction=direction,
        )

        if levels is None:
            self.last_analysis = {
                **features,
                "status": "NO_TRADE",
                "reason": "APEX stop distance exceeds instrument cap or levels are invalid",
                "upper_wick_ratio": upper_wick_ratio,
                "lower_wick_ratio": lower_wick_ratio,
            }
            return None

        stop_loss, target, risk_distance = levels
        actual_rr = abs(target - close) / risk_distance

        reason = (
            f"APEX {direction}: score={score:.3f}, "
            f"gapATR={gap_atr:.2f}, RVOL={rvol:.2f}, "
            f"OIR={features['oir']:.2f}, VWAP confirmation"
        )

        self.last_analysis = {
            **features,
            "status": "SIGNAL",
            "direction": direction,
            "entry": close,
            "stop_loss": stop_loss,
            "target": target,
            "risk_distance": risk_distance,
            "risk_reward": actual_rr,
            "upper_wick_ratio": upper_wick_ratio,
            "lower_wick_ratio": lower_wick_ratio,
            "vwap": vwap,
            "reason": reason,
        }

        return StrategySignal(
            action=action,
            symbol=self.symbol,
            timestamp=timestamp,
            price=float(close),
            stop_loss=float(stop_loss),
            target=float(target),
            reason=reason,
        )

    # -----------------------------------------------------------------
    # Candle processing
    # -----------------------------------------------------------------

    def on_candle(
        self,
        candle: dict,
        vwap: float,
    ) -> Optional[StrategySignal]:
        """
        Process one COMPLETED candle.

        This method manages open positions at all times. Entry gating is
        applied only after position management, so positions remain protected
        after the entry window closes.
        """
        try:
            raw_timestamp = _normalize_ist_naive(candle["datetime"])
            event_timestamp = self._event_timestamp(raw_timestamp)

            working = {
                "datetime": event_timestamp,
                "raw_datetime": raw_timestamp,
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
                "volume": float(candle.get("volume", np.nan)),
            }
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(f"[{self.symbol}] APEX invalid candle: {exc}")
            return None

        if self.current_date is None:
            self.reset_session(event_timestamp.date())
        elif event_timestamp.date() != self.current_date:
            self.reset_session(event_timestamp.date())

        # The first opening candle must be the actual market-open candle.
        if raw_timestamp.time() == self.cfg.market_open and not self._opening_candle_seen:
            self.today_open = working["open"]
            self._opening_candle_seen = True

        # Preserve one normalized raw-time candle only once.
        self.today_bars.append(
            {
                "datetime": raw_timestamp,
                "open": working["open"],
                "high": working["high"],
                "low": working["low"],
                "close": working["close"],
                "volume": working["volume"],
            }
        )

        # -------------------------------------------------------------
        # 1. Manage position FIRST, regardless of entry window.
        # -------------------------------------------------------------
        if self.position != 0:
            if event_timestamp.time() >= self.config.square_off_time:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=event_timestamp,
                    price=working["close"],
                    reason="APEX_TIME_SQUARE_OFF",
                )

            exit_signal = self._check_active_position_exits(working)
            if exit_signal is not None:
                return exit_signal

        # -------------------------------------------------------------
        # 2. Outside the entry window -> no new entry, but diagnostics can
        # still be updated.
        # -------------------------------------------------------------
        features = self._calculate_features(
            working,
            vwap=vwap,
        )

        if not features:
            self.last_analysis = {
                "status": "NO_TRADE",
                "reason": "APEX indicators unavailable",
            }
            return None

        vwap_value = _safe_float(vwap)
        self.last_analysis = {
            **features,
            "vwap": vwap_value,
        }

        if self.regime is not None and not self.regime.trading_enabled:
            self.last_analysis.update(
                status="NO_TRADE",
                reason=self.regime.reason,
            )
            return None

        if event_timestamp.time() < self.cfg.entry_start:
            self.last_analysis.update(
                status="WAITING",
                reason="Before APEX entry window",
            )
            return None

        if event_timestamp.time() > self.cfg.entry_end:
            self.last_analysis.update(
                status="NO_TRADE",
                reason="After APEX entry window",
            )
            return None

        return self._generate_entry_signal(
            working,
            features,
        )

    # -----------------------------------------------------------------
    # Tick processing
    # -----------------------------------------------------------------

    def _maybe_activate_breakeven(self, price: float) -> None:
        if self.trailing_breakeven_active or self.initial_risk_dist <= 0:
            return

        r_multiple = _safe_float(
            getattr(self.config, "breakeven_r_multiple", 1.0)
        ) or 1.0
        if r_multiple <= 0:
            return

        if self.position == 1:
            trigger = self.entry_price + r_multiple * self.initial_risk_dist
            if price >= trigger:
                self.stop_loss = self.entry_price
                self.trailing_breakeven_active = True

        elif self.position == -1:
            trigger = self.entry_price - r_multiple * self.initial_risk_dist
            if price <= trigger:
                self.stop_loss = self.entry_price
                self.trailing_breakeven_active = True

    def on_tick(
        self,
        price: float,
        timestamp: datetime,
    ) -> Optional[StrategySignal]:
        if self.position == 0:
            return None

        px = _safe_float(price)
        if px is None:
            return None

        ts = _normalize_ist_naive(timestamp)

        if ts.time() >= self.config.square_off_time:
            return StrategySignal(
                action=SignalAction.EXIT,
                symbol=self.symbol,
                timestamp=ts,
                price=px,
                reason="APEX_TIME_SQUARE_OFF",
            )

        # Activate BE before checking the current tick against the stop. This
        # handles a tick reaching +1R and then being evaluated at the same
        # price without leaving the old stop behind.
        self._maybe_activate_breakeven(px)

        if self.position == 1:
            if px <= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=px,
                    reason=(
                        "APEX_BREAKEVEN_SL"
                        if self.trailing_breakeven_active
                        else "APEX_STOP_LOSS"
                    ),
                )

            if px >= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=px,
                    reason="APEX_TARGET",
                )

        elif self.position == -1:
            if px >= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=px,
                    reason=(
                        "APEX_BREAKEVEN_SL"
                        if self.trailing_breakeven_active
                        else "APEX_STOP_LOSS"
                    ),
                )

            if px <= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=px,
                    reason="APEX_TARGET",
                )

        return None

    # -----------------------------------------------------------------
    # Candle exit engine
    # -----------------------------------------------------------------

    def _check_active_position_exits(
        self,
        candle: dict,
    ) -> Optional[StrategySignal]:
        """
        Conservative OHLC exit handling.

        LONG:
            open >= target -> target at open
            open <= stop   -> stop at open
            both touched   -> stop first unless OPTIMISTIC

        SHORT mirrors the above.
        """
        timestamp = _normalize_ist_naive(candle["datetime"])
        open_price = float(candle["open"])
        high = float(candle["high"])
        low = float(candle["low"])

        if self.position == 1:
            if open_price >= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=open_price,
                    reason="APEX_TARGET_GAP",
                )

            if open_price <= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=open_price,
                    reason=(
                        "APEX_BREAKEVEN_SL_GAP"
                        if self.trailing_breakeven_active
                        else "APEX_STOP_LOSS_GAP"
                    ),
                )

            hit_target = high >= self.target
            hit_stop = low <= self.stop_loss

            if hit_stop and hit_target:
                if self.execution_policy == "OPTIMISTIC":
                    return StrategySignal(
                        action=SignalAction.EXIT,
                        symbol=self.symbol,
                        timestamp=timestamp,
                        price=self.target,
                        reason="APEX_TARGET",
                    )
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=self.stop_loss,
                    reason=(
                        "APEX_BREAKEVEN_SL"
                        if self.trailing_breakeven_active
                        else "APEX_STOP_LOSS"
                    ),
                )

            if hit_stop:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=self.stop_loss,
                    reason=(
                        "APEX_BREAKEVEN_SL"
                        if self.trailing_breakeven_active
                        else "APEX_STOP_LOSS"
                    ),
                )

            if hit_target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=self.target,
                    reason="APEX_TARGET",
                )

            self._maybe_activate_breakeven(high)
            return None

        if self.position == -1:
            if open_price <= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=open_price,
                    reason="APEX_TARGET_GAP",
                )

            if open_price >= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=open_price,
                    reason=(
                        "APEX_BREAKEVEN_SL_GAP"
                        if self.trailing_breakeven_active
                        else "APEX_STOP_LOSS_GAP"
                    ),
                )

            hit_target = low <= self.target
            hit_stop = high >= self.stop_loss

            if hit_stop and hit_target:
                if self.execution_policy == "OPTIMISTIC":
                    return StrategySignal(
                        action=SignalAction.EXIT,
                        symbol=self.symbol,
                        timestamp=timestamp,
                        price=self.target,
                        reason="APEX_TARGET",
                    )
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=self.stop_loss,
                    reason=(
                        "APEX_BREAKEVEN_SL"
                        if self.trailing_breakeven_active
                        else "APEX_STOP_LOSS"
                    ),
                )

            if hit_stop:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=self.stop_loss,
                    reason=(
                        "APEX_BREAKEVEN_SL"
                        if self.trailing_breakeven_active
                        else "APEX_STOP_LOSS"
                    ),
                )

            if hit_target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=self.target,
                    reason="APEX_TARGET",
                )

            self._maybe_activate_breakeven(low)
            return None

        return None


# Normal project alias.
ApexStrategy = ApexAivemStrategy
