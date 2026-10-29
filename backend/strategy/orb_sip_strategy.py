"""
ORB-SIP: 30-minute Opening Range Breakout, only on "stocks in play".

Research result (67 NSE large caps, Aug-Oct 2026, costs included): did NOT pass.
It lost money in the first half of the test and made money in the second half.
Kept as an experimental strategy; paper trade only.

Rules (15-minute candles)
- Opening range = high/low of the 09:15 and 09:30 candles.
- Only "stocks in play": the 09:15 candle's volume is at least
  MIN_RELATIVE_VOLUME x its 10-day average.
- First candle that CLOSES above the range high -> BUY; below the range low -> SELL.
- Signal candles up to 11:15 (candle open time); one signal a day.
- Stop: middle of the opening range. Target: REWARD_RISK x the risk.
- Anything still open is closed at 15:15.
"""

from __future__ import annotations

from datetime import time
from typing import Dict, Optional

from backend.strategy.base_strategy import SignalAction, StrategySignal
from backend.strategy.intraday_base import IntradaySignalStrategy

RANGE_CANDLES = 2
MIN_RELATIVE_VOLUME = 2.5
REWARD_RISK = 2.0
LAST_SIGNAL_CANDLE = time(11, 15)


class OrbSipStrategy(IntradaySignalStrategy):
    key = "orb_sip"
    display_name = "ORB Stocks-in-Play"

    def on_new_session(self) -> None:
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.in_play: Optional[bool] = None
        self.status_note = "Building the 30-minute opening range (09:15-09:45)."

    def entry(self, bar: dict) -> Optional[StrategySignal]:
        if len(self.today) < RANGE_CANDLES:
            return None
        if self.range_high is None:
            first = self.today[:RANGE_CANDLES]
            if first[0]["datetime"].time().strftime("%H:%M") != "09:15":
                self.status_note = "Missing the 09:15 candle; skipped today."
                self.signalled_today = True
                return None
            self.range_high = max(c["high"] for c in first)
            self.range_low = min(c["low"] for c in first)
            rv = self.relative_volume()
            self.in_play = rv is not None and rv >= MIN_RELATIVE_VOLUME
            if not self.in_play:
                self.status_note = f"Not in play today (volume {rv or 0:.2f}x < {MIN_RELATIVE_VOLUME}x)."
            else:
                self.status_note = f"In play (volume {rv:.1f}x). Waiting for a close outside the range."
            return None  # the range is only known once the 09:30 candle closes

        if not self.in_play:
            return None
        if bar["datetime"].time() > LAST_SIGNAL_CANDLE:
            self.status_note = "No breakout before 11:30; no trade today."
            return None

        price = bar["close"]
        mid = (self.range_high + self.range_low) / 2.0
        if price > self.range_high:
            risk = price - mid
            sig = self._signal(SignalAction.BUY, bar, mid, price + REWARD_RISK * risk,
                               f"Closed above the 30-min range high {self.range_high:.2f} on a high-volume day.")
        elif price < self.range_low:
            risk = mid - price
            sig = self._signal(SignalAction.SELL, bar, mid, price - REWARD_RISK * risk,
                               f"Closed below the 30-min range low {self.range_low:.2f} on a high-volume day.")
        else:
            return None
        if sig is not None:
            self.status_note = "Signal given."
        return sig

    def levels(self) -> Dict[str, float]:
        out = super().levels()
        if self.range_high is not None:
            out["range_high"] = round(self.range_high, 2)
            out["range_low"] = round(self.range_low, 2)
        return out
