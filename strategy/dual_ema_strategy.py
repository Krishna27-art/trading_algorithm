"""
Adaptive Volatility-Buffered Dual-EMA Intraday Trend System.

EMA9/EMA21 crossover with an ATR-scaled no-trade buffer around the EMAs
(to filter out whipsaws) and an SMA200 higher-timeframe trend filter.
Unlike ORB/CPR, the underlying indicators (EMA9, EMA21, SMA200, ATR14) are
continuous/cross-day — they don't reset each session — so this strategy
carries a rolling price-history buffer seeded once via seed_context() and
extended bar-by-bar within the session.

Simplification vs. the source material: the source describes a staged exit
(close half the position at 1.5x ATR, trail the remainder to breakeven).
This repo's trade/cost/position-sizing model (PositionSizer, TransactionCostCalculator,
PerformanceAnalyzer) is built around one quantity in, one exit out per trade
— partial-exit accounting would need real changes there, not just here. This
implementation uses a single fixed stop (1.2x ATR) and target (2.0x ATR) per
the source's own numbers, full position each way. Worth revisiting if this
strategy earns a spot in your live rotation and staged exits turn out to
matter for its real performance.

Timeframe note: same as CPRRegimeBreakoutStrategy — runs on this repo's
native 15-minute candles rather than 5-minute, for consistency with the
rest of the data/backtest pipeline.
"""

from datetime import date, datetime, time
from typing import List, Optional
import pandas as pd

from config.settings import InstrumentConfig, StrategyConfig, settings
from monitoring.logger import logger
from strategy.base_strategy import BaseStrategy, SignalAction, StrategySignal


def _compute_indicators(bars: pd.DataFrame) -> pd.DataFrame:
    """Adds ema9, ema21, sma200, atr14 columns to a copy of `bars`
    (expects columns: high, low, close, in chronological order)."""
    df = bars.copy()
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["sma200"] = df["close"].rolling(window=200, min_periods=1).mean()

    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(window=14, min_periods=1).mean()
    return df


class BufferedDualEMAStrategy(BaseStrategy):
    def __init__(
        self,
        instrument: InstrumentConfig,
        strategy_config: StrategyConfig = settings.strategy,
        buffer_gamma: float = 0.175,      # midpoint of the source's 0.15-0.20 range
        stop_atr_multiple: float = 1.2,
        target_atr_multiple: float = 2.0,
        session_start: time = time(9, 30),
        session_end: time = time(15, 0),
        min_warmup_bars: int = 50,
    ):
        super().__init__(instrument.symbol)
        self.instrument = instrument
        self.config = strategy_config
        self.buffer_gamma = buffer_gamma
        self.stop_atr_multiple = stop_atr_multiple
        self.target_atr_multiple = target_atr_multiple
        self.session_start = session_start
        self.session_end = session_end
        # SMA200 (the binding constraint — EMA9/EMA21/ATR14 all settle well
        # before this) is computed with min_periods=1, so with fewer bars
        # than this it is just an average of what's there, not a real
        # SMA200, and the trend filter it gates is unreliable. Refuse to
        # generate entries until there's enough history for it to mean
        # what its name says.
        self.min_warmup_bars = min_warmup_bars

        self.current_date: Optional[date] = None
        self.warm_history: pd.DataFrame = pd.DataFrame(columns=["datetime", "high", "low", "close"])
        self.today_bars: List[dict] = []

    def seed_context(self, historical_bars: pd.DataFrame) -> None:
        """Stores the trailing bar history used to warm up EMA9/EMA21/SMA200/ATR14
        before today's session starts. More history = a more settled SMA200;
        with fewer than 200 bars available the SMA is still computed (min_periods=1)
        but is really just an average of what's there, not a true 200-period value —
        expect the trend filter to be unreliable for the first ~8 trading days of
        any backtest window."""
        if historical_bars is None or historical_bars.empty:
            self.warm_history = pd.DataFrame(columns=["datetime", "high", "low", "close"])
        else:
            self.warm_history = historical_bars[["datetime", "high", "low", "close"]].copy()

    def reset_session(self, session_date: date):
        self.current_date = session_date
        self.position = 0
        self.entry_price = 0.0
        self.stop_loss = 0.0
        self.target = 0.0
        self.initial_risk_dist = 0.0
        self.trailing_breakeven_active = False
        self.trades_today = 0
        self.today_bars = []  # NOT the warm_history — that's cross-day and stays

    def _current_indicators(self, up_to_candle: dict) -> Optional[pd.Series]:
        self.today_bars.append({
            "datetime": up_to_candle["datetime"],
            "high": up_to_candle["high"],
            "low": up_to_candle["low"],
            "close": up_to_candle["close"],
        })
        combined = pd.concat([self.warm_history, pd.DataFrame(self.today_bars)], ignore_index=True)
        if len(combined) < self.min_warmup_bars:
            return None
        indicators = _compute_indicators(combined)
        return indicators.iloc[-1]

    def on_candle(self, candle: dict, vwap: float) -> Optional[StrategySignal]:
        bar_time = candle["datetime"].time()
        close = candle["close"]

        if bar_time >= self.config.square_off_time and self.position != 0:
            return StrategySignal(action=SignalAction.EXIT, symbol=self.symbol,
                                   timestamp=candle["datetime"], price=close, reason="TIME_SQUARE_OFF")

        if self.position != 0:
            exit_sig = self._check_active_position_exits(candle)
            if exit_sig:
                return exit_sig

        row = self._current_indicators(candle)
        if row is None:
            return None

        if self.position == 0 and self.trades_today == 0:
            if not (self.session_start <= bar_time <= self.session_end):
                return None

            ema9, ema21, sma200, atr14 = row["ema9"], row["ema21"], row["sma200"], row["atr14"]
            buffer = self.buffer_gamma * atr14

            if close > ema9 > (ema21 + buffer) and close > sma200:
                stop = close - (self.stop_atr_multiple * atr14)
                target = close + (self.target_atr_multiple * atr14)
                return StrategySignal(action=SignalAction.BUY, symbol=self.symbol,
                                       timestamp=candle["datetime"], price=close,
                                       stop_loss=stop, target=target, reason="DUAL_EMA_TREND_LONG")

            elif close < ema9 < (ema21 - buffer) and close < sma200:
                stop = close + (self.stop_atr_multiple * atr14)
                target = close - (self.target_atr_multiple * atr14)
                return StrategySignal(action=SignalAction.SELL, symbol=self.symbol,
                                       timestamp=candle["datetime"], price=close,
                                       stop_loss=stop, target=target, reason="DUAL_EMA_TREND_SHORT")

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
