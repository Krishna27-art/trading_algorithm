"""
Shared plumbing for the three intraday strategies (Gap Fade, RSI x EMA, ORB-SIP).

Candle contract (same as the rest of the system):
- 15-minute candles, timestamp = candle OPEN time (Zerodha convention).
- A candle is only acted on once it has closed, i.e. at open + 15 minutes.

What this base class does for every strategy:
- seed_context(): builds prior-session context known BEFORE the day starts
  (previous close, 10-day average daily range "ATR", 10-day average volume of
  the 09:15 candle) so nothing from the future leaks in.
- on_candle(): resets on a new day, manages an open position (stop, target,
  time exit at 15:15) and otherwise asks the subclass for an entry.
- Signal only: nothing here places orders.

Rules shared by all three strategies:
- one signal per stock per day
- stop/target checked on each candle's high/low; if both are touched in the
  same candle the STOP is assumed (worst case)
- a candle that opens beyond the stop/target exits at that open price
- every position is closed by 15:15
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from backend.strategy.base_strategy import BaseStrategy, SignalAction, StrategySignal

CANDLE_MINUTES = 15
SESSION_OPEN = time(9, 15)
SQUARE_OFF = time(15, 15)          # candle-close time at which any position is closed
CONTEXT_SESSIONS = 10              # prior sessions used for ATR and volume averages


def _to_ist_naive(value) -> datetime:
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
    return ts.to_pydatetime()


class IntradaySignalStrategy(BaseStrategy):
    """Base for single-stock, one-signal-a-day intraday strategies."""

    key: str = "base"
    display_name: str = "Base"

    def __init__(self, symbol: str):
        super().__init__(symbol)
        self.current_date: Optional[date] = None
        self.today: List[dict] = []          # today's completed candles (open-time stamps)
        self.signalled_today: bool = False
        self.last_signal: Optional[StrategySignal] = None
        self.last_exit_reason: Optional[str] = None

        # prior-session context
        self.prev_close: Optional[float] = None
        self.daily_atr: Optional[float] = None
        self.first_bar_avg_volume: Optional[float] = None
        self.context_sessions: int = 0

    # ------------------------------------------------------------------ context
    def seed_context(self, historical_bars: pd.DataFrame) -> None:
        self.prev_close = None
        self.daily_atr = None
        self.first_bar_avg_volume = None
        self.context_sessions = 0
        if historical_bars is None or historical_bars.empty:
            return

        df = historical_bars.copy()
        if "datetime" not in df.columns and isinstance(df.index, pd.DatetimeIndex):
            df["datetime"] = df.index
        df["datetime"] = pd.to_datetime(df["datetime"].map(_to_ist_naive))
        df = df.sort_values("datetime")
        df["date"] = df["datetime"].dt.date
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"])
        if df.empty:
            return

        daily = df.groupby("date").agg(h=("high", "max"), l=("low", "min"), c=("close", "last"))
        self.context_sessions = len(daily)
        self.prev_close = float(daily["c"].iloc[-1])

        pc = daily["c"].shift()
        tr = pd.concat(
            [daily["h"] - daily["l"], (daily["h"] - pc).abs(), (daily["l"] - pc).abs()], axis=1
        ).max(axis=1)
        tr = tr.iloc[1:]  # first day has no previous close
        if len(tr) >= CONTEXT_SESSIONS:
            atr = float(tr.iloc[-CONTEXT_SESSIONS:].mean())
            self.daily_atr = atr if np.isfinite(atr) and atr > 0 else None

        minutes = df["datetime"].dt.hour * 60 + df["datetime"].dt.minute
        first = df[minutes == SESSION_OPEN.hour * 60 + SESSION_OPEN.minute].groupby("date")["volume"].sum()
        first = first[first > 0]
        if len(first) >= CONTEXT_SESSIONS:
            avg = float(first.iloc[-CONTEXT_SESSIONS:].mean())
            self.first_bar_avg_volume = avg if avg > 0 else None

    # ------------------------------------------------------------------ session
    def reset_session(self, session_date: date):
        self.current_date = session_date
        self.today = []
        self.signalled_today = False
        self.last_signal = None
        self.last_exit_reason = None
        self.position = 0
        self.entry_price = 0.0
        self.stop_loss = 0.0
        self.target = 0.0
        self.initial_risk_dist = 0.0
        self.trailing_breakeven_active = False
        self.trades_today = 0
        self.on_new_session()

    def on_new_session(self) -> None:
        """Subclass hook for per-day state."""

    # ------------------------------------------------------------------ helpers
    def relative_volume(self) -> Optional[float]:
        """Today's 09:15 candle volume vs its prior 10-session average."""
        if not self.today or self.first_bar_avg_volume is None:
            return None
        first = self.today[0]
        if first["datetime"].time() != SESSION_OPEN:
            return None
        vol = float(first.get("volume") or 0)
        return vol / self.first_bar_avg_volume if vol > 0 else None

    def _signal(self, action: SignalAction, candle: dict, stop: float, target: float, reason: str) -> Optional[StrategySignal]:
        price = float(candle["close"])
        if action == SignalAction.BUY and not (stop < price < target):
            return None
        if action == SignalAction.SELL and not (target < price < stop):
            return None
        self.signalled_today = True
        sig = StrategySignal(
            action=action,
            symbol=self.symbol,
            timestamp=candle["close_time"],
            price=round(price, 2),
            stop_loss=round(float(stop), 2),
            target=round(float(target), 2),
            reason=reason,
            order_type="MARKET",
            product="MIS",
        )
        self.last_signal = sig
        return sig

    def _exit(self, candle: dict, price: float, reason: str) -> StrategySignal:
        self.last_exit_reason = reason
        return StrategySignal(
            action=SignalAction.EXIT,
            symbol=self.symbol,
            timestamp=candle["close_time"],
            price=round(float(price), 2),
            reason=reason,
        )

    def _check_exit(self, candle: dict) -> Optional[StrategySignal]:
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
        if self.position == 1:
            if o <= self.stop_loss:
                return self._exit(candle, o, "STOP_LOSS")
            if self.target and o >= self.target:
                return self._exit(candle, o, "PROFIT_TARGET")
            if l <= self.stop_loss:
                return self._exit(candle, self.stop_loss, "STOP_LOSS")
            if self.target and h >= self.target:
                return self._exit(candle, self.target, "PROFIT_TARGET")
        elif self.position == -1:
            if o >= self.stop_loss:
                return self._exit(candle, o, "STOP_LOSS")
            if self.target and o <= self.target:
                return self._exit(candle, o, "PROFIT_TARGET")
            if h >= self.stop_loss:
                return self._exit(candle, self.stop_loss, "STOP_LOSS")
            if self.target and l <= self.target:
                return self._exit(candle, self.target, "PROFIT_TARGET")
        if self.position != 0 and candle["close_time"].time() >= SQUARE_OFF:
            return self._exit(candle, c, "TIME_SQUARE_OFF")
        return None

    # ------------------------------------------------------------------ events
    def on_candle(self, candle: dict, vwap: float) -> Optional[StrategySignal]:
        open_time = _to_ist_naive(candle["datetime"])
        bar = {
            "datetime": open_time,
            "close_time": open_time + timedelta(minutes=CANDLE_MINUTES),
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle.get("volume") or 0),
            "vwap": float(vwap) if vwap is not None and np.isfinite(vwap) and vwap > 0 else None,
        }
        if self.current_date != open_time.date():
            self.reset_session(open_time.date())
        self.today.append(bar)

        if self.position != 0:
            return self._check_exit(bar)
        if self.signalled_today or bar["close_time"].time() >= SQUARE_OFF:
            self.after_candle(bar)
            return None
        sig = self.entry(bar)
        self.after_candle(bar)
        return sig

    def after_candle(self, bar: dict) -> None:
        """Subclass hook called after every candle (indicator upkeep)."""

    def entry(self, bar: dict) -> Optional[StrategySignal]:  # pragma: no cover - abstract
        raise NotImplementedError

    def on_tick(self, price: float, timestamp: datetime) -> Optional[StrategySignal]:
        if self.position == 0 or price is None or price <= 0:
            return None
        ts = _to_ist_naive(timestamp)
        fake = {"close_time": ts}
        if self.position == 1:
            if price <= self.stop_loss:
                return self._exit(fake, price, "STOP_LOSS")
            if self.target and price >= self.target:
                return self._exit(fake, price, "PROFIT_TARGET")
        else:
            if price >= self.stop_loss:
                return self._exit(fake, price, "STOP_LOSS")
            if self.target and price <= self.target:
                return self._exit(fake, price, "PROFIT_TARGET")
        if ts.time() >= SQUARE_OFF:
            return self._exit(fake, price, "TIME_SQUARE_OFF")
        return None

    # ------------------------------------------------------------------ display
    def levels(self) -> Dict[str, float]:
        """Numbers shown next to the signal in the dashboard."""
        out: Dict[str, float] = {}
        if self.prev_close is not None:
            out["prev_close"] = round(self.prev_close, 2)
        if self.daily_atr is not None:
            out["daily_atr"] = round(self.daily_atr, 2)
        rv = self.relative_volume()
        if rv is not None:
            out["relative_volume"] = round(rv, 2)
        return out
