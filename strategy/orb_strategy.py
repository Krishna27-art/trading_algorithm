"""
Production Implementation: 30-Minute Volatility-Filtered Opening Range Breakout (ORB).
Calibrated for: Nifty 50 Index Futures / Highly Liquid Large-Cap MIS Equities.
"""

from datetime import date, datetime, time
from typing import Dict, List, Optional
import pandas as pd

from config.settings import InstrumentConfig, StrategyConfig, settings
from indicators.orb import ORBCalculator, OpeningRange
from monitoring.logger import logger
from strategy.base_strategy import BaseStrategy, SignalAction, StrategySignal


class IntradayORBStrategy(BaseStrategy):
    def __init__(
        self,
        instrument: InstrumentConfig,
        strategy_config: StrategyConfig = settings.strategy,
    ):
        super().__init__(instrument.symbol)
        self.instrument = instrument
        self.config = strategy_config

        # Daily Session State
        self.current_date: Optional[date] = None
        self.orb: Optional[OpeningRange] = None
        self.trades_today: int = 0
        self.position: int = 0  # +1: Long, -1: Short, 0: Flat
        self.entry_price: float = 0.0
        self.stop_loss: float = 0.0
        self.target: float = 0.0
        self.initial_risk_dist: float = 0.0
        self.trailing_breakeven_active: bool = False
        self.history_today: List[dict] = []

    def reset_session(self, session_date: date):
        """Resets all intraday states before market open (09:15 IST)."""
        self.current_date = session_date
        self.orb = None
        self.trades_today = 0
        self.position = 0
        self.entry_price = 0.0
        self.stop_loss = 0.0
        self.target = 0.0
        self.initial_risk_dist = 0.0
        self.trailing_breakeven_active = False
        self.history_today.clear()
        logger.info(f"[{self.symbol}] Strategy session state reset for {session_date}.")

    def register_trade_entry(self, entry_price: float, position: int, stop_loss: float, target: float, risk_dist: float):
        self.position = position
        self.entry_price = entry_price
        self.stop_loss = stop_loss
        self.target = target
        self.initial_risk_dist = risk_dist
        self.trailing_breakeven_active = False
        self.trades_today += 1

    def register_trade_exit(self):
        self.position = 0
        self.entry_price = 0.0
        self.stop_loss = 0.0
        self.target = 0.0
        self.initial_risk_dist = 0.0
        self.trailing_breakeven_active = False

    def on_candle(self, candle: dict, vwap: float) -> Optional[StrategySignal]:
        """
        Evaluates completed 15-minute candle against ORB rules:
        - Locks opening range at 09:45:00
        - Enforces 14:30 time square-off
        - Evaluates Long/Short signals between 09:45 and 13:30
        """
        bar_time = candle["datetime"].time()
        close = candle["close"]
        high = candle["high"]
        low = candle["low"]
        self.history_today.append(candle)

        # 1. Mandatory Time Square-Off Check at or after 14:30 IST
        if bar_time >= self.config.square_off_time and self.position != 0:
            logger.info(f"[{self.symbol}] Time exit trigger at {bar_time}. Forcing square-off.")
            return StrategySignal(
                action=SignalAction.EXIT,
                symbol=self.symbol,
                timestamp=candle["datetime"],
                price=close,
                reason="TIME_SQUARE_OFF",
            )

        # 2. Manage Active Position Exits & Trailing Stop on candle close
        if self.position != 0:
            exit_sig = self._check_active_position_exits(candle)
            if exit_sig:
                return exit_sig

        # 3. Establish Opening Range between 09:15 and 09:45 IST
        if self.orb is None:
            if bar_time >= time(9, 45):
                df_today = pd.DataFrame(self.history_today)
                self.orb = ORBCalculator.calculate_opening_range(
                    day_15m_bars=df_today,
                    min_orb_range=self.instrument.min_orb_range,
                    max_orb_range=self.instrument.max_orb_range,
                )
                if self.orb:
                    logger.info(
                        f"[{self.symbol}] ORB Established: High={self.orb.high:.2f}, Low={self.orb.low:.2f}, "
                        f"Width={self.orb.width:.2f} pts | Volatility Filter Passed: {self.orb.is_valid_volatility}"
                    )
            return None

        # 4. Filter Check: Skip if opening range failed baseline volatility cutoff (< 40 pts)
        if not self.orb.is_valid_volatility:
            return None

        # 5. Entry Rules: 09:45 to 13:30 IST, strictly 1 trade per day
        if self.position == 0 and self.trades_today == 0:
            if bar_time < self.config.entry_start or bar_time > self.config.entry_end:
                return None

            # LONG ENTRY
            # Condition 1: Completed 15m candle closes strictly above ORB High
            # Condition 2: Closes strictly above session VWAP
            if close > self.orb.high and close > vwap:
                initial_stop = self.orb.low
                raw_risk = close - initial_stop

                # Cap effective risk distance if OR_width > 120
                if self.orb.width > self.instrument.max_orb_range:
                    effective_risk = min(raw_risk, self.instrument.max_risk_cap)
                else:
                    effective_risk = raw_risk

                target = close + (self.config.risk_reward_ratio * effective_risk)

                logger.info(
                    f"[{self.symbol}] LONG SIGNAL: Close {close:.2f} > ORB High {self.orb.high:.2f} "
                    f"& VWAP {vwap:.2f} | Stop: {initial_stop:.2f} | Target: {target:.2f}"
                )

                return StrategySignal(
                    action=SignalAction.BUY,
                    symbol=self.symbol,
                    timestamp=candle["datetime"],
                    price=close,
                    stop_loss=initial_stop,
                    target=target,
                    reason="ORB_LONG_BREAKOUT",
                )

            # SHORT ENTRY
            # Condition 1: Completed 15m candle closes strictly below ORB Low
            # Condition 2: Closes strictly below session VWAP
            elif close < self.orb.low and close < vwap:
                initial_stop = self.orb.high
                raw_risk = initial_stop - close

                if self.orb.width > self.instrument.max_orb_range:
                    effective_risk = min(raw_risk, self.instrument.max_risk_cap)
                else:
                    effective_risk = raw_risk

                target = close - (self.config.risk_reward_ratio * effective_risk)

                logger.info(
                    f"[{self.symbol}] SHORT SIGNAL: Close {close:.2f} < ORB Low {self.orb.low:.2f} "
                    f"& VWAP {vwap:.2f} | Stop: {initial_stop:.2f} | Target: {target:.2f}"
                )

                return StrategySignal(
                    action=SignalAction.SELL,
                    symbol=self.symbol,
                    timestamp=candle["datetime"],
                    price=close,
                    stop_loss=initial_stop,
                    target=target,
                    reason="ORB_SHORT_BREAKDOWN",
                )

        return None

    def on_tick(self, price: float, timestamp: datetime) -> Optional[StrategySignal]:
        """Continuous tick-level evaluation for real-time Stop-Loss and Target hits."""
        if self.position == 0:
            return None

        # Mandatory time square-off at 14:30
        if timestamp.time() >= self.config.square_off_time:
            return StrategySignal(
                action=SignalAction.EXIT,
                symbol=self.symbol,
                timestamp=timestamp,
                price=price,
                reason="TIME_SQUARE_OFF",
            )

        if self.position == 1:  # Long Position
            # Check Trailing Stop to Breakeven (+1R gain reached)
            if not self.trailing_breakeven_active:
                if price >= (self.entry_price + (self.config.breakeven_r_multiple * self.initial_risk_dist)):
                    self.stop_loss = self.entry_price
                    self.trailing_breakeven_active = True
                    logger.info(f"[{self.symbol}] Trailing SL active: Stop moved to Breakeven ({self.stop_loss:.2f}).")

            # Check Stop Loss Hit
            if price <= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=self.stop_loss,
                    reason="BREAKEVEN_SL" if self.trailing_breakeven_active else "STOP_LOSS",
                )

            # Check Profit Target Hit
            if price >= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

        elif self.position == -1:  # Short Position
            # Check Trailing Stop to Breakeven (+1R gain reached)
            if not self.trailing_breakeven_active:
                if price <= (self.entry_price - (self.config.breakeven_r_multiple * self.initial_risk_dist)):
                    self.stop_loss = self.entry_price
                    self.trailing_breakeven_active = True
                    logger.info(f"[{self.symbol}] Trailing SL active: Stop moved to Breakeven ({self.stop_loss:.2f}).")

            # Check Stop Loss Hit
            if price >= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=self.stop_loss,
                    reason="BREAKEVEN_SL" if self.trailing_breakeven_active else "STOP_LOSS",
                )

            # Check Profit Target Hit
            if price <= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

        return None

    def _check_active_position_exits(self, candle: dict) -> Optional[StrategySignal]:
        """Checks candle High/Low for intrabar stop or target triggers."""
        high = candle["high"]
        low = candle["low"]
        close = candle["close"]
        bar_time = candle["datetime"]

        if self.position == 1:
            # Trailing Stop to Breakeven check
            if not self.trailing_breakeven_active:
                if high >= (self.entry_price + (self.config.breakeven_r_multiple * self.initial_risk_dist)):
                    self.stop_loss = self.entry_price
                    self.trailing_breakeven_active = True
                    logger.info(f"[{self.symbol}] Trailing SL active: Stop moved to Breakeven ({self.stop_loss:.2f}).")

            if low <= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=self.stop_loss,
                    reason="BREAKEVEN_SL" if self.trailing_breakeven_active else "STOP_LOSS",
                )
            elif high >= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

        elif self.position == -1:
            if not self.trailing_breakeven_active:
                if low <= (self.entry_price - (self.config.breakeven_r_multiple * self.initial_risk_dist)):
                    self.stop_loss = self.entry_price
                    self.trailing_breakeven_active = True
                    logger.info(f"[{self.symbol}] Trailing SL active: Stop moved to Breakeven ({self.stop_loss:.2f}).")

            if high >= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=self.stop_loss,
                    reason="BREAKEVEN_SL" if self.trailing_breakeven_active else "STOP_LOSS",
                )
            elif low <= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

        return None
