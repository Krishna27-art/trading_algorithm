"""
Generic Strategy-Agnostic Event-Driven Backtester.

EventDrivenBacktester (event_engine.py) has the ORB+VWAP rules hardcoded
directly into its bar loop — it can't backtest anything else. This engine
does the same job (bar-by-bar simulation, zero look-ahead, real position
sizing, real transaction costs, the daily loss kill-switch) but delegates
all entry/exit decisions to a `BaseStrategy` instance via on_candle()/on_tick(),
the same interface the live ExecutionEngine already uses. Any strategy
written against strategy/base_strategy.py — CPRRegimeBreakoutStrategy,
BufferedDualEMAStrategy, a future one — plugs in here without this file
changing.

It also calls strategy.seed_context() once per session with a trailing
window of prior days' bars, before reset_session() — so strategies that need
cross-day context (CPR's prior-day pivots, Dual-EMA's SMA200/EMA warm-up)
get it, and strategies that don't (ORB) just ignore it.
"""

from typing import Callable, List, Optional
import pandas as pd

from backtest.performance import PerformanceAnalyzer, PerformanceReport
from config.settings import AppSettings, InstrumentConfig, settings
from indicators.vwap import calculate_session_vwap
from monitoring.logger import logger
from risk.position_sizer import PositionSizer
from risk.transaction_costs import TransactionCostCalculator
from strategy.base_strategy import BaseStrategy, SignalAction


class StrategyBacktester:
    def __init__(
        self,
        strategy_factory: Callable[[], BaseStrategy],
        instrument: InstrumentConfig,
        app_settings: AppSettings = settings,
        context_lookback_days: int = 30,
    ):
        """
        strategy_factory: a zero-arg callable returning a FRESH BaseStrategy
        instance (e.g. `lambda: CPRRegimeBreakoutStrategy(instrument)`) — a
        factory rather than a shared instance so RollingWalkForwardValidator
        can build an independent strategy per fold with no state bleeding
        across folds.
        """
        self.strategy_factory = strategy_factory
        self.instrument = instrument
        self.settings = app_settings
        self.position_sizer = PositionSizer(app_settings.risk)
        self.cost_calculator = TransactionCostCalculator(app_settings.costs)
        self.context_lookback_days = context_lookback_days

    def run(self, df_15m: pd.DataFrame, initial_capital: float = 1000000.0) -> PerformanceReport:
        trades = self.generate_trades(df_15m, initial_capital=initial_capital)
        return PerformanceAnalyzer.generate_report(trades, initial_capital=initial_capital)

    def generate_trades(self, df_15m: pd.DataFrame, initial_capital: float = 1000000.0) -> List[dict]:
        data = df_15m.copy()
        if "datetime" not in data.columns and isinstance(data.index, pd.DatetimeIndex):
            data["datetime"] = data.index
        data["datetime"] = pd.to_datetime(data["datetime"])
        data.sort_values("datetime", inplace=True)
        data["vwap"] = calculate_session_vwap(data).values
        data["date"] = data["datetime"].dt.date

        days = list(data.groupby("date"))
        strategy = self.strategy_factory()
        current_capital = initial_capital
        all_trades: List[dict] = []

        for idx, (session_date, day_df) in enumerate(days):
            day_df = day_df.copy().reset_index(drop=True)
            if len(day_df) < 3:
                continue

            # Cross-day context: every bar from prior sessions in the lookback
            # window, NEVER including today — this is the no-look-ahead
            # boundary every strategy's seed_context() relies on.
            if idx > 0:
                start = max(0, idx - self.context_lookback_days)
                lookback_df = pd.concat([d for _, d in days[start:idx]], ignore_index=True)
                strategy.seed_context(lookback_df)
            else:
                strategy.seed_context(pd.DataFrame(columns=data.columns))

            strategy.reset_session(session_date)

            trades_today = 0
            position = 0
            trade_record: Optional[dict] = None
            daily_realized_pnl = 0.0
            max_allowed_daily_loss = current_capital * self.settings.risk.max_daily_loss_pct

            for i in range(len(day_df)):
                row = day_df.iloc[i]
                candle = {
                    "datetime": row["datetime"], "open": row["open"], "high": row["high"],
                    "low": row["low"], "close": row["close"], "volume": row.get("volume", 0),
                }
                vwap = row["vwap"]

                if -daily_realized_pnl >= max_allowed_daily_loss:
                    break

                signal = strategy.on_candle(candle, vwap)
                if signal is None:
                    continue

                if signal.action == SignalAction.EXIT and position != 0 and trade_record is not None:
                    self._close_position(trade_record, signal.price, signal.timestamp, signal.reason, all_trades)
                    daily_realized_pnl += trade_record["pnl_net"]
                    current_capital += trade_record["pnl_net"]
                    strategy.register_trade_exit()
                    position = 0
                    trade_record = None
                    continue

                if signal.action in (SignalAction.BUY, SignalAction.SELL) and position == 0 and trades_today == 0:
                    stop_distance = abs(signal.price - signal.stop_loss) if signal.stop_loss else 0.0
                    qty = self.position_sizer.calculate_order_quantity(
                        capital=current_capital,
                        stop_distance=stop_distance,
                        instrument=self.instrument,
                    )
                    if qty <= 0:
                        continue

                    direction = "BUY" if signal.action == SignalAction.BUY else "SELL"
                    position = 1 if direction == "BUY" else -1
                    trades_today += 1
                    trade_record = {
                        "trade_id": f"BT_{session_date}_{trades_today}",
                        "symbol": self.instrument.symbol,
                        "direction": direction,
                        "entry_time": signal.timestamp,
                        "entry_price": signal.price,
                        "quantity": qty,
                        "initial_stop": signal.stop_loss,
                        "initial_target": signal.target,
                        "entry_reason": signal.reason,
                    }
                    strategy.register_trade_entry(
                        entry_price=signal.price, position=position,
                        stop_loss=signal.stop_loss, target=signal.target,
                        risk_dist=stop_distance,
                    )

            # End-of-day cleanup: never carry a position overnight
            if position != 0 and trade_record is not None:
                last_bar = day_df.iloc[-1]
                self._close_position(trade_record, last_bar["close"], last_bar["datetime"], "SESSION_CLOSE", all_trades)
                current_capital += trade_record["pnl_net"]
                strategy.register_trade_exit()

        return all_trades

    def _close_position(self, trade: dict, exit_price: float, exit_time, reason: str, all_trades: List[dict]):
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
            buy_price=buy_p, sell_price=sell_p, quantity=qty,
        )
        net_pnl = round(gross_pnl - costs.total_cost, 2)
        initial_risk_val = abs(trade["entry_price"] - trade["initial_stop"]) * qty if trade.get("initial_stop") else 0.0
        r_mult = round(net_pnl / initial_risk_val, 2) if initial_risk_val > 0 else 0.0

        trade["exit_time"] = exit_time
        trade["exit_price"] = exit_price
        trade["exit_reason"] = reason
        trade["pnl_gross"] = round(gross_pnl, 2)
        trade["pnl_net"] = net_pnl
        trade["total_costs"] = costs.total_cost
        trade["slippage_cost"] = costs.slippage_cost
        trade["r_multiple"] = r_mult

        all_trades.append(trade)
