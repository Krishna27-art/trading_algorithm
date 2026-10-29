"""
RSI x 9 EMA.

Research result (67 NSE large caps, Aug-Oct 2026, costs included): small edge,
positive in both halves of the test period.

Rules (15-minute candles)
- RSI(14) crosses its own 9-period EMA.
- BUY  only if RSI crossed UP while still below 50 and price is above VWAP.
- SELL only if RSI crossed DOWN while still above 50 and price is below VWAP.
- Only on "active" days: the 09:15 candle's volume is at least
  MIN_RELATIVE_VOLUME x its 10-day average.
- Signal candles from 09:45 to 14:00 (candle open time); one signal a day.
- Stop: STOP_ATR x the 10-day average daily range. Target: REWARD_RISK x stop.
- Anything still open is closed at 15:15.
"""

from __future__ import annotations

from datetime import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from backend.strategy.base_strategy import SignalAction, StrategySignal
from backend.strategy.intraday_base import IntradaySignalStrategy

RSI_LENGTH = 14
EMA_LENGTH = 9
MIN_RELATIVE_VOLUME = 1.5
STOP_ATR = 0.3
REWARD_RISK = 3.0
FIRST_SIGNAL_CANDLE = time(9, 45)
LAST_SIGNAL_CANDLE = time(14, 0)


class RsiEmaStrategy(IntradaySignalStrategy):
    key = "rsi_ema"
    display_name = "RSI x EMA"

    def __init__(self, symbol: str):
        super().__init__(symbol)
        self._closes: List[float] = []
        self.rsi: Optional[float] = None
        self.rsi_ema: Optional[float] = None
        self._prev_diff: Optional[float] = None
        self._avg_up: Optional[float] = None
        self._avg_down: Optional[float] = None
        self.status_note = "Warming up RSI."

    # RSI/EMA run continuously across days, so they are rebuilt from history.
    def seed_context(self, historical_bars: pd.DataFrame) -> None:
        super().seed_context(historical_bars)
        self._avg_up = self._avg_down = None
        self._closes = []
        self.rsi = self.rsi_ema = None
        self._prev_diff = None
        self._cross = None
        if historical_bars is None or historical_bars.empty:
            return
        df = historical_bars.copy()
        if "datetime" not in df.columns and isinstance(df.index, pd.DatetimeIndex):
            df["datetime"] = df.index
        df = df.sort_values("datetime")
        for close in pd.to_numeric(df["close"], errors="coerce").dropna():
            self._advance(float(close))

    def _update_indicators(self, close: float) -> None:
        self._closes.append(close)
        if len(self._closes) < 2:
            return
        change = self._closes[-1] - self._closes[-2]
        up, down = max(change, 0.0), max(-change, 0.0)
        n = len(self._closes) - 1
        if self._avg_up is None:
            self._avg_up, self._avg_down = up, down
        else:
            a = 1.0 / RSI_LENGTH
            self._avg_up = self._avg_up + a * (up - self._avg_up)
            self._avg_down = self._avg_down + a * (down - self._avg_down)
        if len(self._closes) > 500:
            self._closes = self._closes[-2:]
        if n < RSI_LENGTH:
            return
        rsi = 100.0 if self._avg_down == 0 else 100.0 - 100.0 / (1.0 + self._avg_up / self._avg_down)
        self.rsi = rsi
        k = 2.0 / (EMA_LENGTH + 1.0)
        self.rsi_ema = rsi if self.rsi_ema is None else self.rsi_ema + k * (rsi - self.rsi_ema)

    def on_new_session(self) -> None:
        self.status_note = "Watching for an RSI cross."

    def on_candle(self, candle: dict, vwap: float) -> Optional[StrategySignal]:
        # The indicator always includes the just-closed candle, even while a
        # position is open, so the RSI never skips bars.
        self._cross = self._advance(float(candle["close"]))
        return super().on_candle(candle, vwap)

    def _advance(self, close: float) -> Optional[float]:
        """Update RSI/EMA with one close; return +1 cross up, -1 cross down, 0 none."""
        before = self._prev_diff
        self._update_indicators(close)
        if self.rsi is None or self.rsi_ema is None:
            self._prev_diff = None
            return None
        self._prev_diff = self.rsi - self.rsi_ema
        if before is None:
            return None
        if before <= 0 < self._prev_diff:
            return 1.0
        if before >= 0 > self._prev_diff:
            return -1.0
        return 0.0

    def entry(self, bar: dict) -> Optional[StrategySignal]:
        cross = getattr(self, "_cross", None)
        if cross is None or cross == 0:
            return None
        t = bar["datetime"].time()
        if t < FIRST_SIGNAL_CANDLE or t > LAST_SIGNAL_CANDLE:
            return None
        if self.daily_atr is None:
            self.status_note = "Daily range history not ready."
            return None
        rv = self.relative_volume()
        if rv is None or rv < MIN_RELATIVE_VOLUME:
            self.status_note = f"Low activity today (volume {rv or 0:.2f}x < {MIN_RELATIVE_VOLUME}x); RSI crosses ignored."
            return None
        if bar["vwap"] is None:
            return None
        price = bar["close"]
        risk = STOP_ATR * self.daily_atr
        if cross > 0 and self.rsi < 50 and price > bar["vwap"]:
            sig = self._signal(SignalAction.BUY, bar, price - risk, price + REWARD_RISK * risk,
                               f"RSI {self.rsi:.1f} crossed above its 9 EMA below 50, price above VWAP, volume {rv:.1f}x.")
        elif cross < 0 and self.rsi > 50 and price < bar["vwap"]:
            sig = self._signal(SignalAction.SELL, bar, price + risk, price - REWARD_RISK * risk,
                               f"RSI {self.rsi:.1f} crossed below its 9 EMA above 50, price below VWAP, volume {rv:.1f}x.")
        else:
            return None
        if sig is not None:
            self.status_note = "Signal given."
        return sig

    def levels(self) -> Dict[str, float]:
        out = super().levels()
        if self.rsi is not None and np.isfinite(self.rsi):
            out["rsi"] = round(self.rsi, 2)
        if self.rsi_ema is not None and np.isfinite(self.rsi_ema):
            out["rsi_ema9"] = round(self.rsi_ema, 2)
        return out
