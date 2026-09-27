"""
APEX-AIVEM Intraday Strategy

APEX is an INDEPENDENT strategy.

It does NOT:
- connect directly to Zerodha
- fetch quotes
- place orders
- modify orders
- cancel orders
- square off positions
- manage broker positions

It receives market candles/context from the existing application pipeline
and produces the same StrategySignal contract used by CPR / Dual EMA / ORB.

Flow:

REAL MARKET DATA
    -> existing data pipeline
    -> APEX
    -> StrategySignal
    -> prediction_service
    -> backend API
    -> frontend
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Callable, Dict, Optional

import numpy as np
import pandas as pd

from config.settings import InstrumentConfig, StrategyConfig, settings
from monitoring.logger import logger
from strategy.base_strategy import BaseStrategy, SignalAction, StrategySignal


# =====================================================================
# CONFIGURATION
# =====================================================================

@dataclass
class EngineConfig:
    # APEX Stage-A style gates
    gap_atr_min: float = 0.60
    gap_atr_max: float = 2.50
    rvol_min: float = 1.80
    cpr_norm_max: float = 0.85

    # Composite weights
    w_gap: float = 0.24
    w_vol: float = 0.19
    w_oir: float = 0.14
    w_sector: float = 0.19
    w_market: float = 0.10
    w_cat: float = 0.14

    # Signal confirmation
    max_wick_ratio: float = 0.35

    # Risk/levels
    stop_atr_mult: float = 0.85
    target_atr_mult: float = 1.50

    # Regime
    vix_halt_below: float = 11.50
    vix_full_deploy_max: float = 22.00
    vix_high_risk_mult: float = 0.50

    # Timing
    entry_start: time = time(9, 30)
    entry_end: time = time(13, 30)

    # History
    atr_period: int = 14
    volume_lookback: int = 20


# =====================================================================
# CATALYST
# =====================================================================

class CatalystScorer:
    """
    Simple deterministic catalyst classifier.

    0 = none
    1 = minor
    2 = material
    3 = major

    The strategy can also receive a precomputed catalyst score through
    set_context(), so live/news infrastructure stays outside APEX.
    """

    _MAJOR_KEYWORDS = (
        "merger",
        "acquisition",
        "acquire",
        "delisting",
        "sebi",
        "regulatory action",
        "index inclusion",
        "index exclusion",
        "ban",
        "fraud",
        "insolvency",
        "resignation of ceo",
        "resignation of md",
    )

    _MATERIAL_KEYWORDS = (
        "results",
        "quarterly results",
        "q1 ",
        "q2 ",
        "q3 ",
        "q4 ",
        "order win",
        "contract win",
        "rating upgrade",
        "rating downgrade",
        "rating change",
        "block deal",
        "bulk deal",
        "stake sale",
        "capacity expansion",
        "dividend",
        "buyback",
    )

    _MINOR_KEYWORDS = (
        "board meeting",
        "investor call",
        "clarification",
        "press release",
    )

    def __init__(self, llm_classify_fn: Optional[Callable] = None):
        self.llm_classify_fn = llm_classify_fn

    def score(self, symbol: str, headlines: list[str]) -> int:
        if not headlines:
            return 0

        if self.llm_classify_fn is not None:
            try:
                return int(self.llm_classify_fn(symbol, headlines))
            except TypeError:
                result = self.llm_classify_fn({symbol: headlines})
                if isinstance(result, dict):
                    return int(result.get(symbol, 0))

        text = " | ".join(str(h).lower() for h in headlines)

        if any(k in text for k in self._MAJOR_KEYWORDS):
            return 3

        if any(k in text for k in self._MATERIAL_KEYWORDS):
            return 2

        if any(k in text for k in self._MINOR_KEYWORDS):
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

    if calendar_blackout:
        return RegimeDecision(
            trading_enabled=False,
            risk_multiplier=0.0,
            reason="Calendar blackout",
        )

    # If VIX is unavailable, do not invent a VIX value.
    if india_vix is None:
        return RegimeDecision(
            trading_enabled=True,
            risk_multiplier=1.0,
            reason="India VIX unavailable; normal strategy evaluation",
        )

    if india_vix < cfg.vix_halt_below:
        return RegimeDecision(
            trading_enabled=False,
            risk_multiplier=0.0,
            reason=f"India VIX {india_vix:.2f} below halt threshold",
        )

    if india_vix <= cfg.vix_full_deploy_max:
        return RegimeDecision(
            trading_enabled=True,
            risk_multiplier=1.0,
            reason=f"India VIX {india_vix:.2f}: normal regime",
        )

    return RegimeDecision(
        trading_enabled=True,
        risk_multiplier=cfg.vix_high_risk_mult,
        reason=f"India VIX {india_vix:.2f}: high-volatility regime",
    )


# =====================================================================
# APEX STRATEGY
# =====================================================================

class ApexAivemStrategy(BaseStrategy):
    """
    Independent APEX-AIVEM strategy.

    Same lifecycle as the other strategies:

        seed_context()
        reset_session()
        on_candle()
        on_tick()

    APEX does not consume CPR, Dual EMA, or ORB outputs.
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

        self.catalyst_scorer = catalyst_scorer or CatalystScorer()

        self.current_date: Optional[date] = None

        # Historical context
        self.warm_history = pd.DataFrame(
            columns=["datetime", "open", "high", "low", "close", "volume"]
        )

        self.today_bars: list[dict] = []

        self.previous_close: Optional[float] = None
        self.today_open: Optional[float] = None

        # External independent market context.
        # This is NOT CPR/EMA/ORB output.
        self.context: Dict[str, Any] = {}

        self.regime: Optional[RegimeDecision] = None

        # Last calculated diagnostic information for the frontend.
        self.last_analysis: Dict[str, Any] = {}

    # -----------------------------------------------------------------
    # External market context
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
        headlines: Optional[list[str]] = None,
    ) -> None:
        """
        Supply independent APEX inputs.

        These inputs may come from market/news infrastructure.

        They must NOT come from CPR, Dual EMA, or ORB predictions.
        """

        if headlines is not None and (catalyst_score is None or catalyst_score == 0):
            catalyst_score = self.catalyst_scorer.score(
                self.symbol,
                headlines,
            )

        self.context = {
            "india_vix": india_vix,
            "calendar_blackout": calendar_blackout,
            "gift_nifty_gap": float(gift_nifty_gap) if gift_nifty_gap is not None else None,
            "sector_rs": float(sector_rs) if sector_rs is not None else None,
            "market_rs": float(market_rs) if market_rs is not None else None,
            "catalyst_score": float(catalyst_score) if catalyst_score is not None else None,
            "cpr_norm": float(cpr_norm) if cpr_norm is not None else None,
            "order_imbalance": order_imbalance,
        }

        self.regime = evaluate_regime(
            self.cfg,
            india_vix=india_vix,
            calendar_blackout=calendar_blackout,
        )

    # -----------------------------------------------------------------
    # Cross-day context
    # -----------------------------------------------------------------

    def seed_context(self, historical_bars: pd.DataFrame) -> None:
        """
        Stores prior-session market data.

        This is used only for APEX calculations.
        """

        if historical_bars is None or historical_bars.empty:
            self.warm_history = pd.DataFrame(
                columns=["datetime", "open", "high", "low", "close", "volume"]
            )
            self.previous_close = None
            return

        data = historical_bars.copy()

        if "datetime" not in data.columns:
            if isinstance(data.index, pd.DatetimeIndex):
                data["datetime"] = data.index
            else:
                raise ValueError("APEX requires datetime column/index")

        data["datetime"] = pd.to_datetime(data["datetime"])
        data.sort_values("datetime", inplace=True)

        required = ["datetime", "open", "high", "low", "close"]

        for column in required:
            if column not in data.columns:
                raise ValueError(f"APEX missing required column: {column}")

        if "volume" not in data.columns:
            data["volume"] = 0

        self.warm_history = data[
            ["datetime", "open", "high", "low", "close", "volume"]
        ].copy()

        self.previous_close = float(
            self.warm_history["close"].iloc[-1]
        )

    # -----------------------------------------------------------------
    # Session reset
    # -----------------------------------------------------------------

    def reset_session(self, session_date: date):
        self.current_date = session_date

        self.position = 0
        self.entry_price = 0.0
        self.stop_loss = 0.0
        self.target = 0.0
        self.initial_risk_dist = 0.0
        self.trailing_breakeven_active = False
        self.trades_today = 0

        self.today_bars = []
        self.today_open = None

        self.last_analysis = {}

    # -----------------------------------------------------------------
    # Indicator calculation
    # -----------------------------------------------------------------

    @staticmethod
    def _calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
        data = df.copy()

        prev_close = data["close"].shift(1)

        tr1 = data["high"] - data["low"]
        tr2 = (data["high"] - prev_close).abs()
        tr3 = (data["low"] - prev_close).abs()

        true_range = pd.concat(
            [tr1, tr2, tr3],
            axis=1,
        ).max(axis=1)

        data["atr14"] = true_range.rolling(
            14,
            min_periods=1,
        ).mean()

        return data

    def _combined_history(self) -> pd.DataFrame:
        today = pd.DataFrame(self.today_bars)

        if self.warm_history.empty:
            return today

        if today.empty:
            return self.warm_history.copy()

        return pd.concat(
            [self.warm_history, today],
            ignore_index=True,
        )

    # -----------------------------------------------------------------
    # Feature calculation
    # -----------------------------------------------------------------

    def _calculate_features(
        self,
        candle: dict,
        vwap: float,
    ) -> Dict[str, float]:

        data = self._combined_history()

        if data.empty:
            return {}

        indicators = self._calculate_indicators(data)
        latest = indicators.iloc[-1]

        atr = float(latest["atr14"])

        if atr <= 0:
            return {}

        close = float(candle["close"])
        high = float(candle["high"])
        low = float(candle["low"])
        open_price = float(candle["open"])
        volume = float(candle.get("volume", 0) or 0)

        if self.today_open is None:
            self.today_open = open_price

        if self.previous_close is None:
            self.previous_close = open_price

        # APEX gap
        gap_atr = (
            (self.today_open - self.previous_close) / atr
            if self.previous_close
            else 0.0
        )

        # Relative volume based on prior bars only.
        prior = self.warm_history

        if not prior.empty and "volume" in prior.columns:
            avg_volume = float(
                prior["volume"]
                .tail(self.cfg.volume_lookback)
                .mean()
            )
        else:
            avg_volume = 0.0

        rvol = (
            volume / avg_volume
            if avg_volume > 0
            else 0.0
        )

        # Actual order imbalance can be supplied by live quote/depth data.
        supplied_oir = self.context.get("order_imbalance")

        if supplied_oir is not None:
            oir = float(supplied_oir)
        else:
            # Fallback candle imbalance for normal candle-only execution.
            # This is deliberately documented as a proxy, not broker OIR.
            candle_range = max(high - low, self.instrument.tick_size)
            oir = (close - open_price) / candle_range
            oir = float(np.clip(oir, -1.0, 1.0))

        # Dynamically accumulate active weights based strictly on verified real inputs
        gap_sign = 1.0 if gap_atr >= 0 else -1.0
        z_gap = float(np.clip(abs(gap_atr) / 1.0, 0.0, 3.0))
        z_vol = float(np.clip(np.log(max(rvol, 0.01)), -3.0, 3.0))
        z_oir = float(np.clip(oir * 3.0, -3.0, 3.0))

        total_active_weight = self.cfg.w_gap + self.cfg.w_vol + self.cfg.w_oir
        raw_weighted_sum = (
            self.cfg.w_gap * z_gap
            + self.cfg.w_vol * z_vol
            + self.cfg.w_oir * (gap_sign * z_oir)
        )

        sector_rs = self.context.get("sector_rs")
        if sector_rs is not None:
            z_sector = float(np.clip(float(sector_rs) * 3.0, -3.0, 3.0))
            raw_weighted_sum += self.cfg.w_sector * (gap_sign * z_sector)
            total_active_weight += self.cfg.w_sector

        market_rs = self.context.get("market_rs")
        if market_rs is not None:
            z_market = float(np.clip(float(market_rs) * 3.0, -3.0, 3.0))
            raw_weighted_sum += self.cfg.w_market * (gap_sign * z_market)
            total_active_weight += self.cfg.w_market

        catalyst = self.context.get("catalyst_score")
        if catalyst is not None and float(catalyst) > 0:
            z_cat = float(np.clip((float(catalyst) / 3.0) * 3.0, 0.0, 3.0))
            raw_weighted_sum += self.cfg.w_cat * z_cat
            total_active_weight += self.cfg.w_cat

        # Normalize composite score by total active weights so missing inputs do not bias as 0
        norm_factor = (1.0 / total_active_weight) if total_active_weight > 0 else 1.0
        composite = gap_sign * (raw_weighted_sum * norm_factor)

        cpr_norm = self.context.get("cpr_norm")
        if cpr_norm is not None:
            cpr_multiplier = 1.0 - (0.10 * float(np.clip(float(cpr_norm), 0.0, 1.5)))
            composite *= cpr_multiplier

        return {
            "atr": atr,
            "gap_atr": gap_atr,
            "rvol": rvol,
            "oir": oir,
            "sector_rs": sector_rs,
            "market_rs": market_rs,
            "catalyst_score": catalyst,
            "cpr_norm": cpr_norm,
            "vwap_distance_pct": (
                ((close - vwap) / vwap) * 100.0
                if vwap
                else 0.0
            ),
            "apex_score": float(composite),
            "close": close,
        }

    def _weight_gap(self, value: float) -> float:
        return self.cfg.w_gap * value

    def _weight_volume(self, value: float) -> float:
        return self.cfg.w_vol * value

    # -----------------------------------------------------------------
    # Main strategy evaluation
    # -----------------------------------------------------------------

    def on_candle(
        self,
        candle: dict,
        vwap: float,
    ) -> Optional[StrategySignal]:

        if self.current_date is None:
            ts = pd.to_datetime(candle["datetime"])
            self.reset_session(ts.date())

        self.today_bars.append(candle)

        bar_time = pd.to_datetime(candle["datetime"]).time()

        # Do not create entries outside APEX's intended intraday window.
        if bar_time < self.cfg.entry_start:
            return None

        if bar_time > self.cfg.entry_end:
            return None

        if self.trades_today > 0:
            return None

        if self.regime is not None and not self.regime.trading_enabled:
            self.last_analysis = {
                "status": "NO_TRADE",
                "reason": self.regime.reason,
            }
            return None

        features = self._calculate_features(
            candle,
            vwap,
        )

        if not features:
            return None

        atr = features["atr"]
        gap_atr = features["gap_atr"]
        rvol = features["rvol"]
        oir = features["oir"]
        score = features["apex_score"]

        # Stage-A style gates.
        if abs(gap_atr) < self.cfg.gap_atr_min:
            self.last_analysis = {
                **features,
                "status": "NO_TRADE",
                "reason": "Gap below APEX minimum threshold",
            }
            return None

        if abs(gap_atr) > self.cfg.gap_atr_max:
            self.last_analysis = {
                **features,
                "status": "NO_TRADE",
                "reason": "Gap above APEX maximum threshold",
            }
            return None

        if rvol > 0 and rvol < self.cfg.rvol_min:
            self.last_analysis = {
                **features,
                "status": "NO_TRADE",
                "reason": "Relative volume below APEX threshold",
            }
            return None

        if features.get("cpr_norm") is not None and features["cpr_norm"] > self.cfg.cpr_norm_max:
            self.last_analysis = {
                **features,
                "status": "NO_TRADE",
                "reason": "CPR-normalization gate failed",
            }
            return None

        # Opening-bar confirmation.
        open_price = float(candle["open"])
        close = float(candle["close"])
        high = float(candle["high"])
        low = float(candle["low"])

        candle_range = max(
            high - low,
            self.instrument.tick_size,
        )

        long_wick_ratio = (high - close) / candle_range
        short_wick_ratio = (close - low) / candle_range

        vwap_long = close > vwap
        vwap_short = close < vwap

        long_confirmation = (
            close > open_price
            and vwap_long
            and long_wick_ratio <= self.cfg.max_wick_ratio
        )

        short_confirmation = (
            close < open_price
            and vwap_short
            and short_wick_ratio <= self.cfg.max_wick_ratio
        )

        direction: Optional[str] = None

        if score > 0 and long_confirmation:
            direction = "LONG"
        elif score < 0 and short_confirmation:
            direction = "SHORT"

        if direction is None:
            self.last_analysis = {
                **features,
                "status": "WAITING",
                "reason": "APEX setup not confirmed",
            }
            return None

        # APEX entry/SL/target.
        if direction == "LONG":
            entry = close
            stop_loss = round(
                entry - self.cfg.stop_atr_mult * atr,
                2,
            )
            target = round(
                entry + self.cfg.target_atr_mult * atr,
                2,
            )

            reason = (
                f"APEX LONG: score={score:.2f}, "
                f"gapATR={gap_atr:.2f}, "
                f"RVOL={rvol:.2f}, "
                f"OIR={oir:.2f}, "
                f"VWAP confirmation"
            )

            action = SignalAction.BUY

        else:
            entry = close
            stop_loss = round(
                entry + self.cfg.stop_atr_mult * atr,
                2,
            )
            target = round(
                entry - self.cfg.target_atr_mult * atr,
                2,
            )

            reason = (
                f"APEX SHORT: score={score:.2f}, "
                f"gapATR={gap_atr:.2f}, "
                f"RVOL={rvol:.2f}, "
                f"OIR={oir:.2f}, "
                f"VWAP confirmation"
            )

            action = SignalAction.SELL

        risk_distance = abs(entry - stop_loss)

        if risk_distance <= 0:
            self.last_analysis = {
                **features,
                "status": "ERROR",
                "reason": "Invalid risk distance",
            }
            return None

        self.last_analysis = {
            **features,
            "status": "SIGNAL",
            "direction": direction,
            "entry": entry,
            "stop_loss": stop_loss,
            "target": target,
            "risk_reward": (
                abs(target - entry) / risk_distance
            ),
            "reason": reason,
        }

        return StrategySignal(
            action=action,
            symbol=self.symbol,
            timestamp=pd.to_datetime(candle["datetime"]).to_pydatetime(),
            price=float(entry),
            stop_loss=float(stop_loss),
            target=float(target),
            reason=reason,
        )

    # -----------------------------------------------------------------
    # Tick monitoring
    # -----------------------------------------------------------------

    def on_tick(
        self,
        price: float,
        timestamp: datetime,
    ) -> Optional[StrategySignal]:

        if self.position == 0:
            return None

        if self.position > 0:
            if self.stop_loss > 0 and price <= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=price,
                    reason="APEX_STOP_LOSS",
                )

            if self.target > 0 and price >= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=price,
                    reason="APEX_TARGET",
                )

        if self.position < 0:
            if self.stop_loss > 0 and price >= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=price,
                    reason="APEX_STOP_LOSS",
                )

            if self.target > 0 and price <= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=price,
                    reason="APEX_TARGET",
                )

        return None


# Normal strategy name used by the rest of the project.
ApexStrategy = ApexAivemStrategy