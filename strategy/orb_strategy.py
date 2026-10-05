"""
Production 30-minute Opening Range Breakout (ORB).

Canonical candle contract:
- Incoming intraday OHLCV timestamps are treated as CANDLE-OPEN timestamps
  (the convention used by Zerodha historical data).
- The strategy evaluates the candle only at its completion time (+15 minutes).
- ORB is formed from 09:15-09:30 and 09:30-09:45 bars.
"""

from datetime import date, datetime, time
from typing import List, Optional

import pandas as pd

from config.settings import InstrumentConfig, InstrumentType, StrategyConfig, settings
from indicators.orb import ORBCalculator, OpeningRange
from monitoring.logger import logger
from strategy.base_strategy import BaseStrategy, SignalAction, StrategySignal


class IntradayORBStrategy(BaseStrategy):
    def __init__(
        self,
        instrument: InstrumentConfig,
        strategy_config: StrategyConfig = settings.strategy,
        execution_policy: str = "CONSERVATIVE",
        candle_timestamps_are_open: bool = True,
    ):
        super().__init__(instrument.symbol)
        self.instrument = instrument
        self.config = strategy_config
        self.execution_policy = getattr(execution_policy, "value", execution_policy)
        self.candle_timestamps_are_open = candle_timestamps_are_open

        self.current_date: Optional[date] = None
        self.orb: Optional[OpeningRange] = None
        self.history_today: List[dict] = []

    @staticmethod
    def _normalize_ist_naive(value) -> datetime:
        """
        Convert an aware timestamp to IST and remove timezone information.
        Naive timestamps are treated as already-IST.
        """
        ts = pd.Timestamp(value)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
        return ts.to_pydatetime()

    def _event_timestamp(self, raw_timestamp) -> datetime:
        """
        Convert a candle-open timestamp into the candle-completion timestamp.
        Zerodha 15m candle stamped 09:15 represents 09:15-09:30.
        """
        ts = self._normalize_ist_naive(raw_timestamp)
        if self.candle_timestamps_are_open:
            ts += pd.Timedelta(minutes=self.config.candle_timeframe_minutes).to_pytimedelta()
        return ts

    def reset_session(self, session_date: date):
        self.current_date = session_date
        self.orb = None
        self.history_today.clear()

        # Position/trade state lives in BaseStrategy.
        self.position = 0
        self.entry_price = 0.0
        self.stop_loss = 0.0
        self.target = 0.0
        self.initial_risk_dist = 0.0
        self.trailing_breakeven_active = False
        self.trades_today = 0

        logger.info(f"[{self.symbol}] ORB session reset for {session_date}.")

    def register_trade_entry(
        self,
        entry_price: float,
        position: int,
        stop_loss: float,
        target: float,
        risk_dist: float,
    ):
        super().register_trade_entry(
            entry_price=entry_price,
            position=position,
            stop_loss=stop_loss,
            target=target,
            risk_dist=risk_dist,
        )

    def register_trade_exit(self):
        super().register_trade_exit()

    def _max_allowed_risk(self, entry_price: float) -> float:
        """
        Return the maximum permitted stop distance.

        Equities use an explicitly configured percentage-of-entry cap.
        Futures/index instruments use the configured absolute point cap.
        """
        tick = max(float(self.instrument.tick_size), 1e-8)

        if self.instrument.instrument_type == InstrumentType.EQUITY:
            pct = self.instrument.equity_orb_max_risk_pct

            if pct is not None and pct > 0:
                return max(entry_price * float(pct), tick)

            cap = float(self.instrument.max_risk_cap)
            if cap > 0:
                return max(cap, tick)

            return max(entry_price * 0.015, tick)

        cap = float(self.instrument.max_risk_cap)

        if cap <= 0:
            return 0.0

        return max(cap, tick)

    def _calculate_opening_range(self) -> Optional[OpeningRange]:
        if not self.history_today:
            return None

        history_df = pd.DataFrame(self.history_today)
        is_equity = self.instrument.instrument_type == InstrumentType.EQUITY

        return ORBCalculator.calculate_opening_range(
            day_15m_bars=history_df,
            min_orb_range=self.instrument.min_orb_range,
            max_orb_range=self.instrument.max_orb_range,
            is_equity=is_equity,
            equity_min_range_pct=getattr(self.instrument, "equity_orb_min_range_pct", None),
            equity_max_range_pct=getattr(self.instrument, "equity_orb_max_range_pct", None),
            timestamps_are_candle_open=self.candle_timestamps_are_open,
        )

    def _entry_signal(
        self,
        candle: dict,
        vwap: float,
    ) -> Optional[StrategySignal]:
        if self.orb is None or not self.orb.is_valid_volatility:
            return None

        bar_time = candle["datetime"].time()
        close = float(candle["close"])

        if self.position != 0 or self.trades_today != 0:
            return None

        if not (self.config.entry_start <= bar_time <= self.config.entry_end):
            return None

        # LONG
        if close > self.orb.high and close > float(vwap):
            stop = float(self.orb.low)
            raw_risk = close - stop
            max_allowed_risk = self._max_allowed_risk(close)

            # Do NOT shrink target-side risk while leaving the real stop far
            # away. If the required stop distance exceeds the hard cap, skip
            # the trade entirely.
            if raw_risk <= 0:
                return None
            if raw_risk > max_allowed_risk:
                logger.info(
                    f"[{self.symbol}] ORB LONG rejected: risk {raw_risk:.2f} "
                    f"> allowed {max_allowed_risk:.2f}."
                )
                return None

            target = close + self.config.risk_reward_ratio * raw_risk

            return StrategySignal(
                action=SignalAction.BUY,
                symbol=self.symbol,
                timestamp=candle["datetime"],
                price=close,
                stop_loss=stop,
                target=target,
                reason="ORB_LONG_BREAKOUT",
            )

        # SHORT
        if close < self.orb.low and close < float(vwap):
            stop = float(self.orb.high)
            raw_risk = stop - close
            max_allowed_risk = self._max_allowed_risk(close)

            if raw_risk <= 0:
                return None
            if raw_risk > max_allowed_risk:
                logger.info(
                    f"[{self.symbol}] ORB SHORT rejected: risk {raw_risk:.2f} "
                    f"> allowed {max_allowed_risk:.2f}."
                )
                return None

            target = close - self.config.risk_reward_ratio * raw_risk

            return StrategySignal(
                action=SignalAction.SELL,
                symbol=self.symbol,
                timestamp=candle["datetime"],
                price=close,
                stop_loss=stop,
                target=target,
                reason="ORB_SHORT_BREAKDOWN",
            )

        return None

    def on_candle(self, candle: dict, vwap: float) -> Optional[StrategySignal]:
        """
        Evaluate one COMPLETED 15-minute candle.

        Input timestamp is normally the candle OPEN time. Internally it is
        converted to the completion time before applying the strategy schedule.
        """
        raw_timestamp = self._normalize_ist_naive(candle["datetime"])
        event_timestamp = self._event_timestamp(raw_timestamp)

        working_candle = dict(candle)
        working_candle["datetime"] = event_timestamp
        working_candle["open"] = float(candle["open"])
        working_candle["high"] = float(candle["high"])
        working_candle["low"] = float(candle["low"])
        working_candle["close"] = float(candle["close"])
        working_candle["volume"] = int(candle.get("volume", 0))

        if self.current_date is None:
            self.reset_session(event_timestamp.date())
        elif event_timestamp.date() != self.current_date:
            # Intraday only: never carry a position across sessions.
            self.reset_session(event_timestamp.date())

        # Keep original candle-open timestamp semantics in history. The ORB
        # calculator explicitly converts them to completion times when needed.
        history_candle = dict(working_candle)
        history_candle["datetime"] = raw_timestamp
        self.history_today.append(history_candle)

        # Time-based exit.
        if event_timestamp.time() >= self.config.square_off_time and self.position != 0:
            return StrategySignal(
                action=SignalAction.EXIT,
                symbol=self.symbol,
                timestamp=event_timestamp,
                price=working_candle["close"],
                reason="TIME_SQUARE_OFF",
            )

        # Position management.
        if self.position != 0:
            exit_sig = self._check_active_position_exits(working_candle)
            if exit_sig:
                return exit_sig

        # Build ORB only after the second opening bar has completed, i.e. at
        # 09:45 completion time.
        if self.orb is None and event_timestamp.time() >= time(9, 45):
            self.orb = self._calculate_opening_range()
            if self.orb:
                logger.info(
                    f"[{self.symbol}] ORB: high={self.orb.high:.2f}, "
                    f"low={self.orb.low:.2f}, width={self.orb.width:.2f}, "
                    f"valid={self.orb.is_valid_volatility}, "
                    f"min={self.orb.min_required:.2f}, max={self.orb.max_allowed:.2f}"
                )

        if self.orb is None or not self.orb.is_valid_volatility:
            return None

        return self._entry_signal(working_candle, float(vwap))

    def on_tick(self, price: float, timestamp: datetime) -> Optional[StrategySignal]:
        """
        Tick timestamps are already event timestamps; do NOT add 15 minutes.
        Used for live stop/target/breakeven monitoring.
        """
        if self.position == 0:
            return None

        ts = self._normalize_ist_naive(timestamp)
        px = float(price)

        if ts.time() >= self.config.square_off_time:
            return StrategySignal(
                action=SignalAction.EXIT,
                symbol=self.symbol,
                timestamp=ts,
                price=px,
                reason="TIME_SQUARE_OFF",
            )

        if self.position == 1:
            if not self.trailing_breakeven_active:
                trigger = self.entry_price + self.config.breakeven_r_multiple * self.initial_risk_dist
                if px >= trigger:
                    self.stop_loss = self.entry_price
                    self.trailing_breakeven_active = True

            if px <= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.stop_loss,
                    reason="BREAKEVEN_SL" if self.trailing_breakeven_active else "STOP_LOSS",
                )

            if px >= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

        elif self.position == -1:
            if not self.trailing_breakeven_active:
                trigger = self.entry_price - self.config.breakeven_r_multiple * self.initial_risk_dist
                if px <= trigger:
                    self.stop_loss = self.entry_price
                    self.trailing_breakeven_active = True

            if px >= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.stop_loss,
                    reason="BREAKEVEN_SL" if self.trailing_breakeven_active else "STOP_LOSS",
                )

            if px <= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

        return None

    def _check_active_position_exits(self, candle: dict) -> Optional[StrategySignal]:
        """
        Candle OHLC stop/target logic with:
        - gap-through handling
        - conservative same-candle collision handling
        - breakeven activation for the NEXT candle
        """
        open_p = float(candle.get("open", candle["close"]))
        high = float(candle["high"])
        low = float(candle["low"])
        bar_time = candle["datetime"]

        if self.position == 1:
            if open_p >= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=open_p,
                    reason="PROFIT_TARGET",
                )
            if open_p <= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=open_p,
                    reason="BREAKEVEN_SL" if self.trailing_breakeven_active else "STOP_LOSS",
                )

            hit_target = high >= self.target
            hit_stop = low <= self.stop_loss

            if hit_target and hit_stop:
                if self.execution_policy == "OPTIMISTIC":
                    return StrategySignal(
                        action=SignalAction.EXIT,
                        symbol=self.symbol,
                        timestamp=bar_time,
                        price=self.target,
                        reason="PROFIT_TARGET",
                    )
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=self.stop_loss,
                    reason="BREAKEVEN_SL" if self.trailing_breakeven_active else "STOP_LOSS",
                )

            if hit_target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

            if hit_stop:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=self.stop_loss,
                    reason="BREAKEVEN_SL" if self.trailing_breakeven_active else "STOP_LOSS",
                )

            if not self.trailing_breakeven_active:
                trigger = self.entry_price + self.config.breakeven_r_multiple * self.initial_risk_dist
                if high >= trigger:
                    self.stop_loss = self.entry_price
                    self.trailing_breakeven_active = True

        elif self.position == -1:
            if open_p <= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=open_p,
                    reason="PROFIT_TARGET",
                )
            if open_p >= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=open_p,
                    reason="BREAKEVEN_SL" if self.trailing_breakeven_active else "STOP_LOSS",
                )

            hit_target = low <= self.target
            hit_stop = high >= self.stop_loss

            if hit_target and hit_stop:
                if self.execution_policy == "OPTIMISTIC":
                    return StrategySignal(
                        action=SignalAction.EXIT,
                        symbol=self.symbol,
                        timestamp=bar_time,
                        price=self.target,
                        reason="PROFIT_TARGET",
                    )
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=self.stop_loss,
                    reason="BREAKEVEN_SL" if self.trailing_breakeven_active else "STOP_LOSS",
                )

            if hit_target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

            if hit_stop:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=self.stop_loss,
                    reason="BREAKEVEN_SL" if self.trailing_breakeven_active else "STOP_LOSS",
                )

            if not self.trailing_breakeven_active:
                trigger = self.entry_price - self.config.breakeven_r_multiple * self.initial_risk_dist
                if low <= trigger:
                    self.stop_loss = self.entry_price
                    self.trailing_breakeven_active = True

        return None
