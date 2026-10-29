"""
Gap Fade.

Research result (67 NSE large caps, 5-min data, Aug-Oct 2026, costs included):
the best of ~260 tested versions; positive in both halves of the test period.

Rules
- The stock opens at least GAP_MIN_PCT away from the previous close.
- Wait for the first 30 minutes (the 09:15 and 09:30 candles).
- Gap UP and price fell over those 30 minutes  -> SELL (short).
  Gap DOWN and price rose over those 30 minutes -> BUY.
- Stop: the high (for a short) / low (for a buy) of the first 30 minutes.
- Target: the previous close (the gap filled).
- Anything still open is closed at 15:15.
"""

from __future__ import annotations

from typing import Dict, Optional

from backend.strategy.base_strategy import SignalAction, StrategySignal
from backend.strategy.intraday_base import IntradaySignalStrategy

GAP_MIN_PCT = 1.0
WINDOW_CANDLES = 2  # 09:15 + 09:30 candles = first 30 minutes


class GapFadeStrategy(IntradaySignalStrategy):
    key = "gap_fade"
    display_name = "Gap Fade"

    def on_new_session(self) -> None:
        self.gap_pct: Optional[float] = None
        self.window_high: Optional[float] = None
        self.window_low: Optional[float] = None
        self.decided: bool = False
        self.status_note: str = "Waiting for the first 30 minutes (09:15-09:45)."

    def entry(self, bar: dict) -> Optional[StrategySignal]:
        if self.decided:
            return None
        if len(self.today) < WINDOW_CANDLES:
            return None
        self.decided = True

        if self.prev_close is None:
            self.status_note = "No previous close available."
            return None
        window = self.today[:WINDOW_CANDLES]
        if window[0]["datetime"].time().strftime("%H:%M") != "09:15":
            self.status_note = "Missing the 09:15 candle; skipped today."
            return None

        day_open = window[0]["open"]
        self.gap_pct = (day_open - self.prev_close) / self.prev_close * 100.0
        self.window_high = max(c["high"] for c in window)
        self.window_low = min(c["low"] for c in window)
        window_close = window[-1]["close"]

        if abs(self.gap_pct) < GAP_MIN_PCT:
            self.status_note = f"Gap {self.gap_pct:+.2f}% is smaller than {GAP_MIN_PCT}%; no trade today."
            return None

        if self.gap_pct > 0:
            if window_close >= day_open:
                self.status_note = f"Gap up {self.gap_pct:+.2f}% but price kept rising; no fade."
                return None
            sig = self._signal(
                SignalAction.SELL, bar, stop=self.window_high, target=self.prev_close,
                reason=f"Gap up {self.gap_pct:+.2f}% is fading after 30 min; target = previous close.",
            )
        else:
            if window_close <= day_open:
                self.status_note = f"Gap down {self.gap_pct:+.2f}% but price kept falling; no fade."
                return None
            sig = self._signal(
                SignalAction.BUY, bar, stop=self.window_low, target=self.prev_close,
                reason=f"Gap down {self.gap_pct:+.2f}% is recovering after 30 min; target = previous close.",
            )
        if sig is None:
            self.status_note = "Gap already filled or stop too close; no trade."
        else:
            self.status_note = "Signal given."
        return sig

    def levels(self) -> Dict[str, float]:
        out = super().levels()
        if self.gap_pct is not None:
            out["gap_pct"] = round(self.gap_pct, 2)
        if self.window_high is not None:
            out["first_30m_high"] = round(self.window_high, 2)
            out["first_30m_low"] = round(self.window_low, 2)
        return out
