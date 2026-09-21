"""
Event-Driven Intraday Backtesting Engine.
Zero look-ahead bias, realistic order fills, slippage, and statutory tax modeling.
"""

from datetime import datetime, time
from typing import Dict, List, Optional
import pandas as pd

from backtest.performance import PerformanceAnalyzer, PerformanceReport
from config.settings import AppSettings, InstrumentConfig, settings
from indicators.orb import ORBCalculator
from indicators.vwap import calculate_session_vwap
from monitoring.logger import logger
from risk.position_sizer import PositionSizer
from risk.transaction_costs import TransactionCostCalculator


class EventDrivenBacktester:
    def __init__(self, instrument: InstrumentConfig, app_settings: AppSettings = settings):
        self.instrument = instrument
        self.settings = app_settings
        self.position_sizer = PositionSizer(app_settings.risk)
        self.cost_calculator = TransactionCostCalculator(app_settings.costs)

    def run(self, df_15m: pd.DataFrame, initial_capital: float = 1000000.0) -> PerformanceReport:
        """
        Runs event-driven backtest over 15-minute historical candles and
        returns the aggregated performance report.
        """
        all_trades = self.generate_trades(df_15m, initial_capital=initial_capital)
        return PerformanceAnalyzer.generate_report(all_trades, initial_capital=initial_capital)

    def generate_trades(self, df_15m: pd.DataFrame, initial_capital: float = 1000000.0) -> List[dict]:
        """
        Same event-driven simulation as run(), but returns the raw trade log
        instead of the aggregated report. Callers that need to concatenate
        trades across multiple windows before computing one combined report
        (e.g. RollingWalkForwardValidator) should call this directly instead
        of re-running the backtest a second time just to get the trade list.
        """
        data = df_15m.copy()
        if "datetime" not in data.columns and isinstance(data.index, pd.DatetimeIndex):
            data["datetime"] = data.index
        data["datetime"] = pd.to_datetime(data["datetime"])
        data.sort_values("datetime", inplace=True)

        # Pre-compute session VWAP anchored to daily open (09:15)
        data["vwap"] = calculate_session_vwap(data).values

        current_capital = initial_capital
        all_trades: List[dict] = []

        # Group by calendar date
        data["date"] = data["datetime"].dt.date
        days = data.groupby("date")

        for session_date, day_df in days:
            day_df = day_df.copy().reset_index(drop=True)
            if len(day_df) < 3:
                continue

            # Daily State
            daily_realized_pnl = 0.0
            trades_today = 0
            position = 0  # +1 Long, -1 Short, 0 Flat
            entry_price = 0.0
            stop_loss = 0.0
            target = 0.0
            initial_risk_dist = 0.0
            trailing_breakeven_active = False
            trade_record: Optional[dict] = None
            orb_locked = False
            orb = None

            max_allowed_daily_loss = current_capital * self.settings.risk.max_daily_loss_pct

            for i in range(len(day_df)):
                row = day_df.iloc[i]
                bar_time = row["datetime"].time()
                open_p = row["open"]
                high_p = row["high"]
                low_p = row["low"]
                close_p = row["close"]
                vwap = row["vwap"]

                # 1. Establish Opening Range at 09:45 (after first two 15m bars: 09:15 and 09:30)
                if not orb_locked and bar_time >= time(9, 45):
                    bars_so_far = day_df.iloc[:i+1].set_index("datetime")
                    orb = ORBCalculator.calculate_opening_range(
                        day_15m_bars=bars_so_far,
                        min_orb_range=self.instrument.min_orb_range,
                        max_orb_range=self.instrument.max_orb_range,
                    )
                    orb_locked = True

                # If ORB not ready or failed volatility cutoff, continue
                if not orb_locked or orb is None or not orb.is_valid_volatility:
                    continue

                # 2. Manage Open Position (Intrabar SL, Target, Breakeven Trailing, and 14:30 Square-Off)
                if position != 0 and trade_record is not None:
                    # A. Time Square-Off at or after 14:30 IST
                    if bar_time >= self.settings.strategy.square_off_time:
                        exit_price = close_p
                        exit_reason = "TIME_SQUARE_OFF"
                        self._close_position(
                            trade_record, exit_price, row["datetime"], exit_reason, all_trades
                        )
                        daily_realized_pnl += trade_record["pnl_net"]
                        current_capital += trade_record["pnl_net"]
                        position = 0
                        trade_record = None
                        continue

                    # B. Active Position Management for LONG
                    if position == 1:
                        # Trailing to Breakeven (+1R reached)
                        if not trailing_breakeven_active:
                            if high_p >= (entry_price + initial_risk_dist):
                                stop_loss = entry_price
                                trailing_breakeven_active = True

                        # Stop Loss Hit
                        if low_p <= stop_loss:
                            exit_price = stop_loss
                            exit_reason = "BREAKEVEN_SL" if trailing_breakeven_active else "STOP_LOSS"
                            self._close_position(
                                trade_record, exit_price, row["datetime"], exit_reason, all_trades
                            )
                            daily_realized_pnl += trade_record["pnl_net"]
                            current_capital += trade_record["pnl_net"]
                            position = 0
                            trade_record = None
                            continue

                        # Target Hit
                        elif high_p >= target:
                            exit_price = target
                            exit_reason = "PROFIT_TARGET"
                            self._close_position(
                                trade_record, exit_price, row["datetime"], exit_reason, all_trades
                            )
                            daily_realized_pnl += trade_record["pnl_net"]
                            current_capital += trade_record["pnl_net"]
                            position = 0
                            trade_record = None
                            continue

                    # C. Active Position Management for SHORT
                    elif position == -1:
                        # Trailing to Breakeven (+1R reached)
                        if not trailing_breakeven_active:
                            if low_p <= (entry_price - initial_risk_dist):
                                stop_loss = entry_price
                                trailing_breakeven_active = True

                        # Stop Loss Hit
                        if high_p >= stop_loss:
                            exit_price = stop_loss
                            exit_reason = "BREAKEVEN_SL" if trailing_breakeven_active else "STOP_LOSS"
                            self._close_position(
                                trade_record, exit_price, row["datetime"], exit_reason, all_trades
                            )
                            daily_realized_pnl += trade_record["pnl_net"]
                            current_capital += trade_record["pnl_net"]
                            position = 0
                            trade_record = None
                            continue

                        # Target Hit
                        elif low_p <= target:
                            exit_price = target
                            exit_reason = "PROFIT_TARGET"
                            self._close_position(
                                trade_record, exit_price, row["datetime"], exit_reason, all_trades
                            )
                            daily_realized_pnl += trade_record["pnl_net"]
                            current_capital += trade_record["pnl_net"]
                            position = 0
                            trade_record = None
                            continue

                # 3. Check 2% Daily Circuit Breaker Kill-Switch
                if -daily_realized_pnl >= max_allowed_daily_loss:
                    break # Cease all trading for today

                # 4. Entry Scanning (09:45 to 13:30 IST), max 1 trade per day
                if position == 0 and trades_today == 0:
                    if bar_time < self.settings.strategy.entry_start or bar_time > self.settings.strategy.entry_end:
                        continue

                    # LONG ENTRY
                    if close_p > orb.high and close_p > vwap:
                        initial_stop = orb.low
                        raw_risk = close_p - initial_stop

                        effective_risk = min(raw_risk, self.instrument.max_risk_cap) if orb.width > self.instrument.max_orb_range else raw_risk
                        qty = self.position_sizer.calculate_order_quantity(
                            capital=current_capital,
                            stop_distance=effective_risk,
                            instrument=self.instrument,
                            or_width=orb.width,
                        )

                        if qty > 0:
                            position = 1
                            entry_price = close_p
                            stop_loss = initial_stop
                            target = close_p + (self.settings.strategy.risk_reward_ratio * effective_risk)
                            initial_risk_dist = effective_risk
                            trades_today += 1
                            trade_record = {
                                "trade_id": f"BT_{session_date}_{trades_today}",
                                "symbol": self.instrument.symbol,
                                "direction": "BUY",
                                "entry_time": row["datetime"],
                                "entry_price": entry_price,
                                "quantity": qty,
                                "initial_stop": stop_loss,
                                "initial_target": target,
                            }

                    # SHORT ENTRY
                    elif close_p < orb.low and close_p < vwap:
                        initial_stop = orb.high
                        raw_risk = initial_stop - close_p

                        effective_risk = min(raw_risk, self.instrument.max_risk_cap) if orb.width > self.instrument.max_orb_range else raw_risk
                        qty = self.position_sizer.calculate_order_quantity(
                            capital=current_capital,
                            stop_distance=effective_risk,
                            instrument=self.instrument,
                            or_width=orb.width,
                        )

                        if qty > 0:
                            position = -1
                            entry_price = close_p
                            stop_loss = initial_stop
                            target = close_p - (self.settings.strategy.risk_reward_ratio * effective_risk)
                            initial_risk_dist = effective_risk
                            trades_today += 1
                            trade_record = {
                                "trade_id": f"BT_{session_date}_{trades_today}",
                                "symbol": self.instrument.symbol,
                                "direction": "SELL",
                                "entry_time": row["datetime"],
                                "entry_price": entry_price,
                                "quantity": qty,
                                "initial_stop": stop_loss,
                                "initial_target": target,
                            }

            # End of day cleanup (ensure no overnight positions)
            if position != 0 and trade_record is not None:
                last_bar = day_df.iloc[-1]
                self._close_position(trade_record, last_bar["close"], last_bar["datetime"], "SESSION_CLOSE", all_trades)
                current_capital += trade_record["pnl_net"]

        return all_trades

    def _close_position(self, trade: dict, exit_price: float, exit_time: datetime, reason: str, all_trades: List[dict]):
        qty = trade["quantity"]
        direction = trade["direction"]

        if direction == "BUY":
            gross_pnl = (exit_price - trade["entry_price"]) * qty
            buy_p, sell_p = trade["entry_price"], exit_price
        else:
            gross_pnl = (trade["entry_price"] - exit_price) * qty
            buy_p, sell_p = exit_price, trade["entry_price"]

        costs = self.cost_calculator.calculate_trade_costs(
            symbol=self.instrument.symbol,
            instrument_type=self.instrument.instrument_type,
            buy_price=buy_p,
            sell_price=sell_p,
            quantity=qty,
        )
        net_pnl = round(gross_pnl - costs.total_cost, 2)
        initial_risk_val = abs(trade["entry_price"] - trade["initial_stop"]) * qty
        r_mult = round(net_pnl / initial_risk_val, 2) if initial_risk_val > 0 else 0.0

        trade["exit_time"] = exit_time
        trade["exit_price"] = exit_price
        trade["exit_reason"] = reason
        trade["pnl_gross"] = round(gross_pnl, 2)
        trade["pnl_net"] = net_pnl
        trade["total_costs"] = costs.total_cost
        trade["r_multiple"] = r_mult

        all_trades.append(trade)
