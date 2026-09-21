"""
Central Pivot Range (CPR) Regime Breakout & Mean-Reversion Strategy.

Adapted from the classic floor-pivot CPR framework (Pivot Point P, Bottom
Central Pivot BC, Top Central Pivot TC, derived from the PRIOR session's
High/Low/Close), with a volatility-regime filter: the CPR width
(|TC-BC|/P * 100) is compared against its own trailing 20-day distribution
to decide whether today is likely to trend (narrow CPR -> breakout rules) or
chop (wide CPR -> mean-reversion-fading rules).

Adaptation note: the source material describes this on 5-minute candles.
This repo's data pipeline, ORB strategy, and backtest engine are all built
around 15-minute candles end to end (HistoricalDataLoader, EventDrivenBacktester,
the live ExecutionEngine's candle aggregator). Rather than bolt on a second,
inconsistent bar size, this implementation runs the same logic on 15-minute
candles — the CPR levels and regime logic are timeframe-independent; only
the entry-window granularity is coarser. If you specifically want 5-minute
signals, the candle timeframe is a config value (StrategyConfig.candle_timeframe_minutes)
and CandleAggregator already supports it — the strategy logic here doesn't
need to change.
"""

from datetime import date, datetime, time
from typing import Dict, List, Optional
import pandas as pd

from config.settings import InstrumentConfig, StrategyConfig, settings
from monitoring.logger import logger
from strategy.base_strategy import BaseStrategy, SignalAction, StrategySignal


class Regime:
    NARROW = "NARROW"   # width <= 20th percentile -> trend/breakout day
    WIDE = "WIDE"        # width >= 80th percentile -> range/fade day
    NEUTRAL = "NEUTRAL"  # in between -> sit out, per the source spec


def _compute_pivots(high: float, low: float, close: float) -> Dict[str, float]:
    p = (high + low + close) / 3.0
    bc = (high + low) / 2.0
    tc = (p - bc) + p  # == 2p - bc
    r1 = 2 * p - low
    s1 = 2 * p - high
    width_pct = (abs(tc - bc) / p) * 100 if p else 0.0
    return {"P": p, "BC": bc, "TC": tc, "R1": r1, "S1": s1, "width_pct": width_pct,
            "prior_high": high, "prior_low": low, "prior_close": close}


class CPRRegimeBreakoutStrategy(BaseStrategy):
    def __init__(
        self,
        instrument: InstrumentConfig,
        strategy_config: StrategyConfig = settings.strategy,
        entry_window_start: time = time(9, 30),
        entry_window_end: time = time(11, 30),
        width_lookback_days: int = 20,
    ):
        super().__init__(instrument.symbol)
        self.instrument = instrument
        self.config = strategy_config
        self.entry_window_start = entry_window_start
        self.entry_window_end = entry_window_end
        self.width_lookback_days = width_lookback_days

        self.current_date: Optional[date] = None
        self.pivots: Optional[Dict[str, float]] = None
        self.regime: str = Regime.NEUTRAL
        self.width_percentile: Optional[float] = None

    def seed_context(self, historical_bars: pd.DataFrame) -> None:
        """Derives prior-day OHLC and the daily CPR-width regime from a
        trailing window of intraday bars. Only ever looks at bars strictly
        before the session being traded — the caller (StrategyBacktester)
        is responsible for that boundary, same as it is for every other
        strategy in this repo."""
        if historical_bars is None or historical_bars.empty:
            self.pivots = None
            self.regime = Regime.NEUTRAL
            return

        data = historical_bars.copy()
        data["date"] = pd.to_datetime(data["datetime"]).dt.date
        daily = data.groupby("date").agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
        daily = daily.sort_index()

        if daily.empty:
            self.pivots = None
            self.regime = Regime.NEUTRAL
            return

        # Prior day = most recent day in the lookback window
        prior_row = daily.iloc[-1]
        self.pivots = _compute_pivots(prior_row["high"], prior_row["low"], prior_row["close"])

        # Width distribution across every day we have pivots for in the window
        widths = []
        for _, row in daily.iterrows():
            widths.append(_compute_pivots(row["high"], row["low"], row["close"])["width_pct"])
        window = widths[-self.width_lookback_days:]

        if len(window) < 5:  # not enough history yet for a meaningful percentile
            self.regime = Regime.NEUTRAL
            self.width_percentile = None
        else:
            today_width = window[-1]
            pct_rank = (sum(w < today_width for w in window) / len(window)) * 100
            self.width_percentile = pct_rank
            if pct_rank <= 20:
                self.regime = Regime.NARROW
            elif pct_rank >= 80:
                self.regime = Regime.WIDE
            else:
                self.regime = Regime.NEUTRAL

        logger.info(
            f"[{self.symbol}] CPR context: P={self.pivots['P']:.2f} BC={self.pivots['BC']:.2f} "
            f"TC={self.pivots['TC']:.2f} width={self.pivots['width_pct']:.2f}% "
            f"(percentile={self.width_percentile}) -> regime={self.regime}"
        )

    def reset_session(self, session_date: date):
        self.current_date = session_date
        self.position = 0
        self.entry_price = 0.0
        self.stop_loss = 0.0
        self.target = 0.0
        self.initial_risk_dist = 0.0
        self.trailing_breakeven_active = False
        self.trades_today = 0
        # self.pivots / self.regime deliberately NOT cleared here — they're
        # set once per session by seed_context() and describe TODAY's regime.

    def on_candle(self, candle: dict, vwap: float) -> Optional[StrategySignal]:
        bar_time = candle["datetime"].time()
        high, low, close = candle["high"], candle["low"], candle["close"]

        # 1. Mandatory time square-off
        if bar_time >= self.config.square_off_time and self.position != 0:
            return StrategySignal(action=SignalAction.EXIT, symbol=self.symbol,
                                   timestamp=candle["datetime"], price=close, reason="TIME_SQUARE_OFF")

        # 2. Manage an open position (intrabar stop/target)
        if self.position != 0:
            exit_sig = self._check_active_position_exits(candle)
            if exit_sig:
                return exit_sig

        if self.pivots is None or self.regime == Regime.NEUTRAL:
            return None  # no prior-day context yet, or today isn't a qualifying regime

        # 3. Entry scanning, one trade per day
        if self.position == 0 and self.trades_today == 0:
            p, bc, tc = self.pivots["P"], self.pivots["BC"], self.pivots["TC"]
            prior_range = self.pivots["prior_high"] - self.pivots["prior_low"]

            if self.regime == Regime.NARROW:
                if not (self.entry_window_start <= bar_time <= self.entry_window_end):
                    return None
                if close > tc:
                    target = p + prior_range
                    return StrategySignal(action=SignalAction.BUY, symbol=self.symbol,
                                           timestamp=candle["datetime"], price=close,
                                           stop_loss=p, target=target, reason="CPR_NARROW_BREAKOUT_LONG")
                elif close < bc:
                    target = p - prior_range
                    return StrategySignal(action=SignalAction.SELL, symbol=self.symbol,
                                           timestamp=candle["datetime"], price=close,
                                           stop_loss=p, target=target, reason="CPR_NARROW_BREAKDOWN_SHORT")

            elif self.regime == Regime.WIDE:
                # Mean-reversion fading: buy a pullback holding at BC, short a rally failing at TC
                if low <= bc and close > bc:
                    return StrategySignal(action=SignalAction.BUY, symbol=self.symbol,
                                           timestamp=candle["datetime"], price=close,
                                           stop_loss=bc - (p - bc), target=tc, reason="CPR_WIDE_FADE_LONG_AT_BC")
                elif high >= tc and close < tc:
                    return StrategySignal(action=SignalAction.SELL, symbol=self.symbol,
                                           timestamp=candle["datetime"], price=close,
                                           stop_loss=tc + (tc - p), target=bc, reason="CPR_WIDE_FADE_SHORT_AT_TC")

        return None

    def on_tick(self, price: float, timestamp: datetime) -> Optional[StrategySignal]:
        if self.position == 0:
            return None
        if timestamp.time() >= self.config.square_off_time:
            return StrategySignal(action=SignalAction.EXIT, symbol=self.symbol,
                                   timestamp=timestamp, price=price, reason="TIME_SQUARE_OFF")
        if self.position == 1:
            if price <= self.stop_loss:
                return StrategySignal(action=SignalAction.EXIT, symbol=self.symbol,
                                       timestamp=timestamp, price=self.stop_loss, reason="STOP_LOSS")
            if price >= self.target:
                return StrategySignal(action=SignalAction.EXIT, symbol=self.symbol,
                                       timestamp=timestamp, price=self.target, reason="PROFIT_TARGET")
        elif self.position == -1:
            if price >= self.stop_loss:
                return StrategySignal(action=SignalAction.EXIT, symbol=self.symbol,
                                       timestamp=timestamp, price=self.stop_loss, reason="STOP_LOSS")
            if price <= self.target:
                return StrategySignal(action=SignalAction.EXIT, symbol=self.symbol,
                                       timestamp=timestamp, price=self.target, reason="PROFIT_TARGET")
        return None

    def _check_active_position_exits(self, candle: dict) -> Optional[StrategySignal]:
        high, low = candle["high"], candle["low"]
        bar_time = candle["datetime"]

        if self.position == 1:
            if low <= self.stop_loss:
                return StrategySignal(action=SignalAction.EXIT, symbol=self.symbol,
                                       timestamp=bar_time, price=self.stop_loss, reason="STOP_LOSS")
            elif high >= self.target:
                return StrategySignal(action=SignalAction.EXIT, symbol=self.symbol,
                                       timestamp=bar_time, price=self.target, reason="PROFIT_TARGET")
        elif self.position == -1:
            if high >= self.stop_loss:
                return StrategySignal(action=SignalAction.EXIT, symbol=self.symbol,
                                       timestamp=bar_time, price=self.stop_loss, reason="STOP_LOSS")
            elif low <= self.target:
                return StrategySignal(action=SignalAction.EXIT, symbol=self.symbol,
                                       timestamp=bar_time, price=self.target, reason="PROFIT_TARGET")
        return None
