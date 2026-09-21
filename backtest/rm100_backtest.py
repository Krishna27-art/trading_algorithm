"""
EOD portfolio backtester for NSE-RM-100.

backtest/strategy_backtester.py drives one BaseStrategy instance through
intraday bars for a single instrument; it cannot represent a 10-name
delivery book. This walks daily sessions instead, rebalancing on the
strategy's own cadence and marking the book to the daily close.

Non-look-ahead by construction: on session t the strategy only ever sees
`panel.loc[:t_minus_1]`, and orders fill at day t's close (the 15:00-15:10
execution window in the spec) with slippage applied.

Survivorship: pass `universe_by_date` so each rebalance sees the index
membership of that day. Falling back to a single static list is allowed for
a quick smoke run but will flatter the results; the warning below is not
decorative.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from risk.portfolio_costs import DeliveryCostCalculator
from strategy.portfolio_base import OrderSide
from strategy.residual_momentum import ResidualMomentumStrategy


@dataclass
class Holding:
    quantity: int = 0
    entry_price: float = 0.0
    stop_loss: float = 0.0
    trail_armed: bool = False


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    trades: pd.DataFrame
    rebalances: int
    total_costs: float
    stats: Dict[str, float] = field(default_factory=dict)


class RM100Backtester:
    def __init__(
        self,
        strategy: ResidualMomentumStrategy,
        initial_capital: float = 1_000_000.0,
        cost_calculator: Optional[DeliveryCostCalculator] = None,
    ):
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.costs = cost_calculator or DeliveryCostCalculator()

    def run(
        self,
        closes: pd.DataFrame,
        highs: pd.DataFrame,
        lows: pd.DataFrame,
        volumes: pd.DataFrame,
        index_close: pd.Series,
        universe_by_date: Optional[Dict[date, Sequence[str]]] = None,
        static_universe: Optional[Sequence[str]] = None,
        rf_yield_pct: float = 6.5,
        warmup: Optional[int] = None,
    ) -> BacktestResult:
        cfg = self.strategy.config
        warmup = warmup or (cfg.regression_window + 5)
        if universe_by_date is None and static_universe is None:
            raise ValueError("Pass universe_by_date (point-in-time) or static_universe")
        if universe_by_date is None:
            print("[!] Static universe in use — results carry survivorship bias.")

        sessions = list(closes.index)
        cash = self.initial_capital
        book: Dict[str, Holding] = {}
        equity_points: List[float] = []
        equity_index: List[date] = []
        trade_rows: List[dict] = []
        total_costs = 0.0
        rebalances = 0

        for i in range(warmup, len(sessions)):
            today = sessions[i]
            t_date = today.date() if hasattr(today, "date") else today
            hist = slice(None, sessions[i - 1])  # everything through t-1

            c_hist = closes.loc[hist]
            h_hist = highs.loc[hist]
            l_hist = lows.loc[hist]
            v_hist = volumes.loc[hist]
            i_hist = index_close.loc[hist]

            equity = cash + sum(
                pos.quantity * float(closes[s].loc[today])
                for s, pos in book.items()
                if s in closes.columns and not np.isnan(closes[s].loc[today])
            )

            # ---- daily exit monitoring (stop / trail) -------------------
            exits = self.strategy.monitor(
                session_date=t_date,
                holdings={
                    s: {
                        "quantity": p.quantity,
                        "entry_price": p.entry_price,
                        "stop_loss": p.stop_loss,
                        "trail_armed": p.trail_armed,
                    }
                    for s, p in book.items()
                },
                closes=c_hist,
                intraday_lows={
                    s: float(lows[s].loc[today]) for s in book if s in lows.columns
                },
            )
            for sig in exits:
                pos = book.get(sig.symbol)
                if not pos or pos.quantity <= 0:
                    continue
                fill = sig.price
                line = self.costs.calculate(pos.entry_price, fill, pos.quantity)
                pnl = (fill - pos.entry_price) * pos.quantity - line.total_cost
                cash += fill * pos.quantity - line.total_cost
                total_costs += line.total_cost
                trade_rows.append({
                    "date": t_date, "symbol": sig.symbol, "side": "SELL",
                    "qty": pos.quantity, "price": fill, "reason": sig.reason,
                    "net_pnl": round(pnl, 2), "costs": line.total_cost,
                })
                del book[sig.symbol]

            # ---- scheduled rebalance -----------------------------------
            if self.strategy.is_rebalance_day(t_date):
                universe = (
                    list(universe_by_date.get(t_date, []))
                    if universe_by_date is not None
                    else list(static_universe)
                )
                if universe:
                    try:
                        plan = self.strategy.generate_plan(
                            as_of=t_date,
                            closes=c_hist, highs=h_hist, lows=l_hist, volumes=v_hist,
                            index_close=i_hist,
                            universe=universe,
                            capital=equity,
                            rf_yield_pct=rf_yield_pct,
                            current_holdings={s: p.quantity for s, p in book.items()},
                            current_equity=equity,
                        )
                    except ValueError:
                        plan = None

                    if plan and not plan.skipped:
                        rebalances += 1
                        stops = {t.symbol: (t.stop_loss or 0.0) for t in plan.targets}
                        # Sells first: they release the cash the buys consume.
                        for order in sorted(plan.orders, key=lambda o: o.side != OrderSide.SELL):
                            if order.symbol not in closes.columns:
                                continue
                            px = float(closes[order.symbol].loc[today])
                            if not np.isfinite(px) or px <= 0:
                                continue
                            if order.side == OrderSide.SELL:
                                pos = book.get(order.symbol)
                                if not pos:
                                    continue
                                qty = min(order.quantity, pos.quantity)
                                line = self.costs.calculate(pos.entry_price, px, qty)
                                pnl = (px - pos.entry_price) * qty - line.total_cost
                                cash += px * qty - line.total_cost
                                total_costs += line.total_cost
                                pos.quantity -= qty
                                if pos.quantity <= 0:
                                    del book[order.symbol]
                                trade_rows.append({
                                    "date": t_date, "symbol": order.symbol, "side": "SELL",
                                    "qty": qty, "price": px, "reason": order.reason,
                                    "net_pnl": round(pnl, 2), "costs": line.total_cost,
                                })
                            else:
                                cost_est = px * order.quantity * 1.002
                                if cost_est > cash:
                                    qty = int(cash // (px * 1.002))
                                else:
                                    qty = order.quantity
                                if qty <= 0:
                                    continue
                                cash -= px * qty
                                pos = book.get(order.symbol, Holding())
                                new_qty = pos.quantity + qty
                                pos.entry_price = (
                                    (pos.entry_price * pos.quantity + px * qty) / new_qty
                                )
                                pos.quantity = new_qty
                                pos.stop_loss = stops.get(order.symbol, pos.stop_loss)
                                book[order.symbol] = pos
                                trade_rows.append({
                                    "date": t_date, "symbol": order.symbol, "side": "BUY",
                                    "qty": qty, "price": px, "reason": order.reason,
                                    "net_pnl": 0.0, "costs": 0.0,
                                })

            equity = cash + sum(
                p.quantity * float(closes[s].loc[today])
                for s, p in book.items()
                if s in closes.columns
            )
            equity_points.append(equity)
            equity_index.append(t_date)

        curve = pd.Series(equity_points, index=pd.Index(equity_index, name="date"))
        return BacktestResult(
            equity_curve=curve,
            trades=pd.DataFrame(trade_rows),
            rebalances=rebalances,
            total_costs=round(total_costs, 2),
            stats=self._stats(curve),
        )

    @staticmethod
    def _stats(curve: pd.Series) -> Dict[str, float]:
        if len(curve) < 2:
            return {}
        rets = curve.pct_change().dropna()
        ann_ret = (curve.iloc[-1] / curve.iloc[0]) ** (252.0 / len(curve)) - 1.0
        vol = float(rets.std(ddof=1)) * np.sqrt(252.0)
        downside = float(rets[rets < 0].std(ddof=1)) * np.sqrt(252.0) if (rets < 0).any() else 0.0
        dd = float(((curve / curve.cummax()) - 1.0).min())
        return {
            "annual_return": round(float(ann_ret), 4),
            "annual_vol": round(vol, 4),
            "sharpe": round(float(ann_ret / vol), 3) if vol else 0.0,
            "sortino": round(float(ann_ret / downside), 3) if downside else 0.0,
            "max_drawdown": round(dd, 4),
            "calmar": round(float(ann_ret / abs(dd)), 3) if dd else 0.0,
        }
