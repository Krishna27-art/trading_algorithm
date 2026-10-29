"""
ORB Retest (research / backtest only).

An NSE adaptation of the "15-minute opening range, close-breakout, enter on
retest" pattern, with ATR and relative-volume (RVOL) quality filters:

  1. Opening range = the first 15-minute candle (09:15-09:30 IST).
  2. A completed candle that CLOSES beyond the range arms a setup
     (above the high -> long, below the low -> short), but only when
       * the range width is a sane fraction of the daily ATR, and
       * the breakout candle's volume is high versus the same time-of-day
         candle over prior sessions (RVOL).
  3. Entry is taken only when a later candle RETESTS the broken level
     (trades back to it) and still closes on the breakout side.
  4. Stop just beyond the retest candle's extreme; target = R:R x risk.
     Exits, gap handling and the time square-off reuse IntradayORBStrategy.

It is deliberately NOT part of the live consensus. Prove it first with the
walk-forward + Monte Carlo endpoints on real history.

Missing history (no ATR / no RVOL baseline) means no trade - never a guess.
Index instruments carry no traded volume, so the RVOL filter blocks them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from typing import Dict, Optional

import numpy as np
import pandas as pd

from backend.config.settings import InstrumentConfig, StrategyConfig, settings
from backend.strategy.base_strategy import SignalAction, StrategySignal
from backend.strategy.orb_strategy import IntradayORBStrategy


@dataclass
class ORBRetestConfig:
    rvol_min: float = 1.2              # breakout volume vs same-slot median
    rvol_lookback_sessions: int = 20
    rvol_min_sessions: int = 10        # fewer sessions -> no RVOL baseline -> no trade
    atr_period: int = 14               # daily ATR (from 15m history)
    min_range_atr: float = 0.10        # opening range too narrow -> noise
    max_range_atr: float = 0.80        # opening range too wide -> move already done
    min_risk_atr: float = 0.05         # stop closer than this -> skip (noise stop)
    retest_max_bars: int = 6           # retest must come within N candles of the breakout
    risk_reward: float = 2.0


class ORBRetestStrategy(IntradayORBStrategy):
    def __init__(
        self,
        instrument: InstrumentConfig,
        strategy_config: StrategyConfig = settings.strategy,
        params: Optional[ORBRetestConfig] = None,
        execution_policy: str = "CONSERVATIVE",
    ):
        super().__init__(instrument, strategy_config, execution_policy=execution_policy)
        self.p = params or ORBRetestConfig()
        self._history: pd.DataFrame = pd.DataFrame()
        self.daily_atr: Optional[float] = None
        self._slot_volume: Dict[str, float] = {}
        self._range: Optional[Dict[str, float]] = None
        self._armed: Optional[Dict[str, float]] = None
        self.last_reason: str = ""

    # ------------------------------------------------------------- context
    def seed_context(self, historical_bars: pd.DataFrame) -> None:
        if historical_bars is None or historical_bars.empty:
            self._history = pd.DataFrame()
            return
        df = historical_bars.copy()
        df["datetime"] = pd.to_datetime(df["datetime"])
        self._history = df.sort_values("datetime").reset_index(drop=True)

    def _compute_context(self, session_date: date) -> None:
        self.daily_atr = None
        self._slot_volume = {}
        h = self._history
        if h.empty:
            return
        h = h[h["datetime"].dt.date < session_date]
        if h.empty:
            return

        daily = h.groupby(h["datetime"].dt.date).agg(
            high=("high", "max"), low=("low", "min"), close=("close", "last")
        )
        if len(daily) >= self.p.atr_period + 1:
            prev_close = daily["close"].shift(1)
            tr = pd.concat(
                [daily["high"] - daily["low"],
                 (daily["high"] - prev_close).abs(),
                 (daily["low"] - prev_close).abs()],
                axis=1,
            ).max(axis=1).dropna()
            atr = float(tr.tail(self.p.atr_period).mean())
            if np.isfinite(atr) and atr > 0:
                self.daily_atr = atr

        recent_days = sorted(h["datetime"].dt.date.unique())[-self.p.rvol_lookback_sessions:]
        recent = h[h["datetime"].dt.date.isin(recent_days)]
        if "volume" in recent.columns:
            slots = recent.assign(slot=recent["datetime"].dt.strftime("%H:%M"))
            for slot, g in slots.groupby("slot"):
                vols = pd.to_numeric(g["volume"], errors="coerce")
                vols = vols[vols > 0]
                if len(vols) >= self.p.rvol_min_sessions:
                    self._slot_volume[slot] = float(vols.median())

    def reset_session(self, session_date: date):
        super().reset_session(session_date)
        self._range = None
        self._armed = None
        self.last_reason = ""
        self._compute_context(session_date)

    # ------------------------------------------------------------- signals
    def _rvol(self, raw_ts, volume: float) -> Optional[float]:
        base = self._slot_volume.get(pd.Timestamp(raw_ts).strftime("%H:%M"))
        if not base or volume is None or volume <= 0:
            return None
        return float(volume) / base

    def on_candle(self, candle: dict, vwap: float) -> Optional[StrategySignal]:
        raw_ts = self._normalize_ist_naive(candle["datetime"])
        event_ts = self._event_timestamp(raw_ts)
        if self.current_date is None or event_ts.date() != self.current_date:
            self.reset_session(event_ts.date())

        c = {
            "datetime": event_ts,
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle.get("volume") or 0.0),
        }
        self.history_today.append(dict(c, datetime=raw_ts))

        if event_ts.time() >= self.config.square_off_time and self.position != 0:
            return StrategySignal(action=SignalAction.EXIT, symbol=self.symbol, timestamp=event_ts,
                                  price=c["close"], reason="TIME_SQUARE_OFF")
        if self.position != 0:
            return self._check_active_position_exits(c)

        # 1. Opening range = the 09:15 candle.
        if self._range is None:
            if raw_ts.time() == time(9, 15):
                self._range = {"high": c["high"], "low": c["low"], "width": c["high"] - c["low"]}
            return None

        if self.trades_today >= self.config.max_trades_per_instrument_day:
            return None
        if not (time(9, 45) <= event_ts.time() <= self.config.entry_end):
            self._armed = None
            return None

        atr = self.daily_atr
        if atr is None:
            self.last_reason = "no daily ATR (insufficient history)"
            return None
        width = self._range["width"]
        if not (self.p.min_range_atr * atr <= width <= self.p.max_range_atr * atr):
            self.last_reason = f"opening range {width:.2f} outside ATR band"
            return None

        hi, lo = self._range["high"], self._range["low"]
        tick = max(float(getattr(self.instrument, "tick_size", 0.05)), 1e-6)

        # 3. Retest of an armed level.
        if self._armed is not None:
            self._armed["bars"] += 1
            side, level = self._armed["side"], self._armed["level"]
            if self._armed["bars"] > self.p.retest_max_bars:
                self._armed = None
                self.last_reason = "retest window expired"
            elif side == 1 and c["close"] < lo:
                self._armed = None
                self.last_reason = "breakout failed (closed below range)"
            elif side == -1 and c["close"] > hi:
                self._armed = None
                self.last_reason = "breakdown failed (closed above range)"
            elif side == 1 and c["low"] <= level < c["close"]:
                stop = c["low"] - tick
                return self._entry(SignalAction.BUY, c, stop, atr, "ORB_RETEST_LONG")
            elif side == -1 and c["high"] >= level > c["close"]:
                stop = c["high"] + tick
                return self._entry(SignalAction.SELL, c, stop, atr, "ORB_RETEST_SHORT")
            return None

        # 2. Close-breakout with RVOL confirmation arms a setup.
        side = 1 if c["close"] > hi else (-1 if c["close"] < lo else 0)
        if side == 0:
            return None
        rvol = self._rvol(raw_ts, c["volume"])
        if rvol is None:
            self.last_reason = "no RVOL baseline / no volume"
            return None
        if rvol < self.p.rvol_min:
            self.last_reason = f"breakout RVOL {rvol:.2f} < {self.p.rvol_min}"
            return None
        self._armed = {"side": side, "level": hi if side == 1 else lo, "bars": 0, "rvol": rvol}
        return None

    def _entry(self, action, c, stop, atr, reason) -> Optional[StrategySignal]:
        price = c["close"]
        risk = (price - stop) if action == SignalAction.BUY else (stop - price)
        self._armed = None
        if risk <= 0 or risk < self.p.min_risk_atr * atr:
            self.last_reason = "retest stop too tight versus ATR"
            return None
        target = price + self.p.risk_reward * risk if action == SignalAction.BUY else price - self.p.risk_reward * risk
        return StrategySignal(action=action, symbol=self.symbol, timestamp=c["datetime"], price=price,
                              stop_loss=stop, target=target, reason=reason)
