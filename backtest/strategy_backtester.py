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

Fix 2.5: RiskManager is now the single source of truth for the daily kill-
switch and per-symbol trade-limit logic instead of being re-implemented
inline here.

Fix 2.4: DB write path wired — trades are persisted to SQLite on entry and
exit via DatabaseManager.

Fix 2.2: accepts an optional pretrain_df so RollingWalkForwardValidator can
supply the full training window as strategy context before test-block
evaluation begins (no-look-ahead boundary is maintained — pretrain rows are
never used to generate trades, only to warm up the strategy state).
"""

import uuid
from datetime import datetime as _dt
from typing import Callable, List, Optional
import pandas as pd

from backtest.performance import PerformanceAnalyzer, PerformanceReport
from config.settings import AppSettings, InstrumentConfig, settings
from database.db import DatabaseManager
from database.models import ExitReason, OrderDirection, TradeRecord
from indicators.vwap import calculate_session_vwap
from monitoring.logger import logger
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from risk.transaction_costs import TransactionCostCalculator
from strategy.base_strategy import BaseStrategy, SignalAction


class StrategyBacktester:
    def __init__(
        self,
        strategy_factory: Callable[[], BaseStrategy],
        instrument: InstrumentConfig,
        app_settings: AppSettings = settings,
        context_lookback_days: int = 30,
        db: Optional[DatabaseManager] = None,
        persist_trades: bool = True,
    ):
        """
        strategy_factory: a zero-arg callable returning a FRESH BaseStrategy
        instance — a factory rather than a shared instance so
        RollingWalkForwardValidator can build an independent strategy per fold
        with no state bleeding across folds.

        db: optional DatabaseManager for persisting trades. Defaults to
        DatabaseManager(app_settings.db_path) when persist_trades=True.

        persist_trades: set False in unit tests to skip DB I/O.
        """
        self.strategy_factory = strategy_factory
        self.instrument = instrument
        self.settings = app_settings
        self.position_sizer = PositionSizer(app_settings.risk)
        self.cost_calculator = TransactionCostCalculator(app_settings.costs)
        self.context_lookback_days = context_lookback_days
        self.persist_trades = persist_trades
        self._db = db  # may be None; lazily resolved in generate_trades

    def _get_db(self) -> Optional[DatabaseManager]:
        if not self.persist_trades:
            return None
        if self._db is None:
            try:
                self._db = DatabaseManager(self.settings.db_path)
            except Exception as e:
                logger.warning(f"Could not open trade DB at {self.settings.db_path}: {e}")
        return self._db

    def run(self, df_15m: pd.DataFrame, initial_capital: float = 1000000.0) -> PerformanceReport:
        trades = self.generate_trades(df_15m, initial_capital=initial_capital)
        return PerformanceAnalyzer.generate_report(trades, initial_capital=initial_capital)

    def generate_trades(
        self,
        df_15m: pd.DataFrame,
        initial_capital: float = 1000000.0,
        pretrain_df: Optional[pd.DataFrame] = None,
    ) -> List[dict]:
        """
        Bar-by-bar backtest simulation.

        pretrain_df (Fix 2.2): if supplied, strategy.seed_context() is called
        with this DataFrame BEFORE processing any bar from df_15m. Used by
        RollingWalkForwardValidator to warm-up strategy state from the full
        train window without leaking future bars.
        """
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
        db = self._get_db()

        # Fix 2.2: warm-up strategy with pre-training data before the test window
        if pretrain_df is not None and not pretrain_df.empty:
            pt = pretrain_df.copy()
            if "datetime" not in pt.columns and isinstance(pt.index, pd.DatetimeIndex):
                pt["datetime"] = pt.index
            pt["datetime"] = pd.to_datetime(pt["datetime"])
            pt.sort_values("datetime", inplace=True)
            strategy.seed_context(pt)

        # Fix 2.5: instantiate RiskManager as the single kill-switch authority
        risk_manager = RiskManager(self.settings.risk, max_portfolio_daily_trades=1)

        for idx, (session_date, day_df) in enumerate(days):
            day_df = day_df.copy().reset_index(drop=True)
            if len(day_df) < 3:
                continue

            # Fix 2.5: reset RiskManager state for the new session
            risk_manager.reset_daily_state(session_date)

            # Cross-day context from df_15m (in addition to any pretrain_df already supplied)
            if idx > 0 and pretrain_df is None:
                # Only apply internal lookback when there's no external pretrain window.
                start = max(0, idx - self.context_lookback_days)
                lookback_df = pd.concat([d for _, d in days[start:idx]], ignore_index=True)
                strategy.seed_context(lookback_df)
            elif idx == 0 and pretrain_df is None:
                strategy.seed_context(pd.DataFrame(columns=data.columns))

            strategy.reset_session(session_date)

            position = 0
            trade_record: Optional[dict] = None
            daily_realized_pnl = 0.0

            for i in range(len(day_df)):
                row = day_df.iloc[i]
                candle = {
                    "datetime": row["datetime"], "open": row["open"], "high": row["high"],
                    "low": row["low"], "close": row["close"], "volume": row.get("volume", 0),
                }
                vwap = row["vwap"]

                # Fix 2.5: delegate kill-switch check to RiskManager
                if risk_manager.kill_switch_active:
                    break

                signal = strategy.on_candle(candle, vwap)
                if signal is None:
                    continue

                if signal.action == SignalAction.EXIT and position != 0 and trade_record is not None:
                    self._close_position(trade_record, signal.price, signal.timestamp, signal.reason, all_trades)
                    pnl_delta = trade_record["pnl_net"]
                    daily_realized_pnl += pnl_delta
                    current_capital += pnl_delta
                    # Fix 2.5: update RiskManager with realized P&L
                    risk_manager.update_pnl(realized_pnl_delta=pnl_delta, capital=current_capital)
                    # Fix 2.4: persist trade exit to DB
                    if db:
                        self._db_persist_trade(db, trade_record, closed=True)
                    strategy.register_trade_exit()
                    position = 0
                    trade_record = None
                    continue

                if signal.action in (SignalAction.BUY, SignalAction.SELL) and position == 0:
                    stop_distance = abs(signal.price - signal.stop_loss) if signal.stop_loss else 0.0
                    qty = self.position_sizer.calculate_order_quantity(
                        capital=current_capital,
                        stop_distance=stop_distance,
                        instrument=self.instrument,
                    )

                    # Fix 2.5: delegate pre-trade validation to RiskManager
                    approved, rejection_reason = risk_manager.validate_pre_trade(
                        symbol=self.instrument.symbol,
                        current_time=candle["datetime"].time() if hasattr(candle["datetime"], "time") else candle["datetime"],
                        quantity=qty,
                        capital=current_capital,
                        has_open_position=(position != 0),
                    )
                    if not approved:
                        continue

                    direction = "BUY" if signal.action == SignalAction.BUY else "SELL"
                    position = 1 if direction == "BUY" else -1
                    trade_id = f"BT_{session_date}_{uuid.uuid4().hex[:8]}"
                    trade_record = {
                        "trade_id": trade_id,
                        "symbol": self.instrument.symbol,
                        "direction": direction,
                        "entry_time": signal.timestamp,
                        "entry_price": signal.price,
                        "quantity": qty,
                        "initial_stop": signal.stop_loss,
                        "initial_target": signal.target,
                        "entry_reason": signal.reason,
                    }
                    # Fix 2.5: record executed trade in RiskManager
                    risk_manager.record_trade_executed(self.instrument.symbol)
                    # Fix 2.4: persist trade entry to DB
                    if db:
                        self._db_persist_trade(db, trade_record, closed=False)
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
                # Fix 2.4: persist session-close exit
                if db:
                    self._db_persist_trade(db, trade_record, closed=True)
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

    @staticmethod
    def _db_persist_trade(db: DatabaseManager, trade: dict, closed: bool):
        """Persist a trade record to the SQLite journal (Fix 2.4)."""
        try:
            direction = OrderDirection.BUY if trade["direction"] == "BUY" else OrderDirection.SELL
            entry_time = trade["entry_time"]
            if isinstance(entry_time, pd.Timestamp):
                entry_time = entry_time.to_pydatetime()

            rec = TradeRecord(
                trade_id=trade["trade_id"],
                symbol=trade["symbol"],
                direction=direction,
                entry_time=entry_time,
                entry_price=float(trade["entry_price"]),
                quantity=int(trade["quantity"]),
                initial_stop=float(trade.get("initial_stop") or 0.0),
                initial_target=float(trade.get("initial_target") or 0.0),
                is_paper=True,
                notes=trade.get("entry_reason", ""),
            )

            if closed and trade.get("exit_time") is not None:
                exit_time = trade["exit_time"]
                if isinstance(exit_time, pd.Timestamp):
                    exit_time = exit_time.to_pydatetime()
                rec.exit_time = exit_time
                rec.exit_price = float(trade.get("exit_price", 0.0))
                rec.pnl_gross = float(trade.get("pnl_gross", 0.0))
                rec.pnl_net = float(trade.get("pnl_net", 0.0))
                rec.total_costs = float(trade.get("total_costs", 0.0))
                rec.slippage = float(trade.get("slippage_cost", 0.0))
                rec.r_multiple = float(trade.get("r_multiple", 0.0))
                exit_reason_str = trade.get("exit_reason", "")
                try:
                    rec.exit_reason = ExitReason(exit_reason_str)
                except ValueError:
                    rec.exit_reason = ExitReason.MANUAL
                db.record_trade_exit(rec)
            else:
                db.record_trade_entry(rec)

        except Exception as e:
            logger.warning(f"DB persist failed for trade {trade.get('trade_id')}: {e}")
