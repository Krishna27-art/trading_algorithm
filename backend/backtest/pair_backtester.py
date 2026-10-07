"""
Cross-Sectional Relative Value (CRSD) Pair & Basket Backtester.

Simulates simultaneous multi-leg pair execution with exact transaction costs
on all legs (primary target + hedge basket), pair-level P&L tracking,
strict zero overnight carry (intraday MIS), and coordinated exits.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence
import pandas as pd

from dataclasses import dataclass, field
from backend.backtest.performance import PerformanceAnalyzer, PerformanceReport
from backend.backtest.strategy_backtester import TransactionCostCalculator
from backend.config.settings import AppSettings, InstrumentConfig, InstrumentType, RiskConfig, settings
from backend.indicators.vwap import calculate_session_vwap
from backend.monitoring.logger import logger
from backend.strategy.base_strategy import SignalAction, StrategySignal
from backend.strategy.crsd_strategy import CRSDPeerContext, CRSDStrategy


@dataclass
class PairPositionSize:
    target_symbol: str
    target_quantity: int
    target_action: str
    hedge_quantities: Dict[str, int] = field(default_factory=dict)
    hedge_actions: Dict[str, str] = field(default_factory=dict)
    total_risk_allocated: float = 0.0
    target_notional: float = 0.0
    hedge_notionals: Dict[str, float] = field(default_factory=dict)
    risk_scale: float = 1.0


class PairPositionSizer:
    def __init__(self, risk_config: RiskConfig = settings.risk):
        self.risk_config = risk_config

    def calculate_pair_quantities(
        self,
        capital: float,
        target_symbol: str,
        target_price: float,
        target_stop_distance: float,
        target_action: str,
        hedge_legs: Dict[str, float],
        hedge_prices: Dict[str, float],
        risk_scale: float = 1.0,
        available_margin: Optional[float] = None,
        margin_requirement_pct: Optional[float] = None,
    ) -> PairPositionSize:
        clean_target = target_symbol.strip().upper()
        if (
            capital <= 0
            or target_price <= 0
            or target_stop_distance <= 0
            or not hedge_legs
        ):
            return PairPositionSize(
                target_symbol=clean_target,
                target_quantity=0,
                target_action=target_action,
                risk_scale=risk_scale,
            )

        scale = max(0.1, min(float(risk_scale), 1.0))
        risk_budget = capital * self.risk_config.risk_per_trade_pct * scale

        raw_target_units = risk_budget / target_stop_distance
        target_qty = int(math.floor(raw_target_units))
        if target_qty < 1:
            return PairPositionSize(
                target_symbol=clean_target,
                target_quantity=0,
                target_action=target_action,
                risk_scale=scale,
            )

        target_notional = target_qty * target_price

        hedge_quantities: Dict[str, int] = {}
        hedge_actions: Dict[str, str] = {}
        hedge_notionals: Dict[str, float] = {}

        for sym, weight in hedge_legs.items():
            clean_sym = sym.strip().upper()
            px = hedge_prices.get(clean_sym, 0.0)
            if px <= 0:
                logger.warning(
                    f"[PairPositionSizer] Missing or invalid price for hedge leg {clean_sym} ({px})."
                )
                continue

            notional_k = target_notional * abs(weight)
            qty_k = int(round(notional_k / px))
            if qty_k < 1:
                qty_k = 1

            action_k = "BUY" if weight > 0 else "SELL"
            hedge_quantities[clean_sym] = qty_k
            hedge_actions[clean_sym] = action_k
            hedge_notionals[clean_sym] = qty_k * px

        if available_margin is not None and self.risk_config.enforce_margin_check:
            if margin_requirement_pct is None or margin_requirement_pct <= 0:
                logger.warning(
                    "[PairPositionSizer] Margin check requested but authoritative "
                    "margin_requirement_pct is missing; failing closed to 0."
                )
                return PairPositionSize(
                    target_symbol=clean_target,
                    target_quantity=0,
                    target_action=target_action,
                    risk_scale=scale,
                )
            total_notional = target_notional + sum(hedge_notionals.values())
            required_margin = total_notional * margin_requirement_pct
            if required_margin > available_margin and total_notional > 0:
                ratio = available_margin / required_margin
                target_qty = max(0, int(math.floor(target_qty * ratio)))
                target_notional = target_qty * target_price
                for s in list(hedge_quantities.keys()):
                    hedge_quantities[s] = max(0, int(math.floor(hedge_quantities[s] * ratio)))
                    hedge_notionals[s] = hedge_quantities[s] * hedge_prices.get(s, 0.0)
                logger.warning(
                    f"[PairPositionSizer] Pair size clamped by available margin ratio {ratio:.2f}."
                )

        return PairPositionSize(
            target_symbol=clean_target,
            target_quantity=target_qty,
            target_action=target_action,
            hedge_quantities=hedge_quantities,
            hedge_actions=hedge_actions,
            total_risk_allocated=round(target_qty * target_stop_distance, 2),
            target_notional=round(target_notional, 2),
            hedge_notionals={k: round(v, 2) for k, v in hedge_notionals.items()},
            risk_scale=scale,
        )


pair_position_sizer = PairPositionSizer()



class PairBacktester:
    """
    Event-driven pair/basket backtester for CRSD statistical arbitrage strategies.
    """

    def __init__(
        self,
        strategy_factory: Callable[[], CRSDStrategy],
        instrument: InstrumentConfig,
        peer_context: CRSDPeerContext,
        app_settings: AppSettings = settings,
        context_lookback_days: int = 30,
    ):
        self.strategy_factory = strategy_factory
        self.instrument = instrument
        self.peer_context = peer_context
        self.settings = app_settings
        self.sizer = pair_position_sizer
        self.cost_calculator = TransactionCostCalculator(app_settings.costs)
        self.context_lookback_days = context_lookback_days

    def run(
        self,
        df_15m: pd.DataFrame,
        initial_capital: float = 1000000.0,
    ) -> PerformanceReport:
        trades = self.generate_trades(df_15m, initial_capital=initial_capital)
        return PerformanceAnalyzer.generate_report(trades, initial_capital=initial_capital)

    def generate_trades(
        self,
        df_15m: pd.DataFrame,
        initial_capital: float = 1000000.0,
        pretrain_df: Optional[pd.DataFrame] = None,
    ) -> List[dict]:
        """
        Simulate pair trading bar-by-bar across trading sessions.
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

        # Track active multi-leg position
        active_trade: Optional[Dict[str, Any]] = None

        for day_idx, (session_date, day_df) in enumerate(days):
            # Seed lookback context
            if pretrain_df is not None and not pretrain_df.empty:
                strategy.seed_context(pretrain_df)
            elif day_idx > 0:
                lookback_days = days[max(0, day_idx - self.context_lookback_days):day_idx]
                lookback_data = pd.concat([d for _, d in lookback_days], ignore_index=True)
                strategy.seed_context(lookback_data)

            strategy.reset_session(session_date)

            for _, row in day_df.iterrows():
                ts = row["datetime"]
                candle = {
                    "datetime": ts,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume", 0)),
                }
                vwap = float(row.get("vwap", candle["close"]))
                price = candle["close"]

                # 1. If currently in a position, evaluate ticks and manage
                if active_trade is not None:
                    # Check tick exits
                    tick_sig = strategy.on_tick(price, ts)
                    candle_sig = strategy.on_candle(candle, vwap)
                    exit_sig = tick_sig or candle_sig

                    if exit_sig is not None and exit_sig.action == SignalAction.EXIT:
                        # Close all legs together
                        trade_record = self._close_pair_trade(
                            active_trade=active_trade,
                            exit_signal=exit_sig,
                            exit_timestamp=ts,
                            target_exit_price=float(exit_sig.price or price),
                        )
                        all_trades.append(trade_record)
                        current_capital += trade_record["net_pnl"]
                        strategy.register_trade_exit()
                        active_trade = None
                        continue

                # 2. If flat, check for new entry signals
                if active_trade is None and strategy.position == 0:
                    sig = strategy.on_candle(candle, vwap)
                    if sig is not None and sig.action in (SignalAction.BUY, SignalAction.SELL):
                        hedge_legs = sig.hedge_legs or getattr(strategy, "hedge_legs", {})
                        if hedge_legs:
                            # Get current prices for all hedge legs at ts
                            hedge_prices: Dict[str, float] = {}
                            for h_sym in hedge_legs.keys():
                                h_bar = self.peer_context.bar(h_sym, ts)
                                if h_bar and h_bar.get("close", 0) > 0:
                                    hedge_prices[h_sym] = float(h_bar["close"])

                            if len(hedge_prices) == len(hedge_legs):
                                # Size multi-leg position
                                stop_dist = abs(sig.price - (sig.stop_loss or sig.price))
                                if stop_dist <= 0:
                                    stop_dist = sig.price * 0.01

                                pos_size = self.sizer.calculate_pair_quantities(
                                    capital=current_capital,
                                    target_symbol=self.instrument.symbol,
                                    target_price=sig.price,
                                    target_stop_distance=stop_dist,
                                    target_action="BUY" if sig.action == SignalAction.BUY else "SELL",
                                    hedge_legs=hedge_legs,
                                    hedge_prices=hedge_prices,
                                    risk_scale=getattr(strategy, "risk_scale", 1.0),
                                )

                                if pos_size.target_quantity > 0:
                                    active_trade = {
                                        "entry_timestamp": ts,
                                        "target_symbol": self.instrument.symbol,
                                        "target_action": pos_size.target_action,
                                        "target_entry_price": sig.price,
                                        "target_quantity": pos_size.target_quantity,
                                        "target_stop_loss": sig.stop_loss,
                                        "target_target": sig.target,
                                        "hedge_quantities": pos_size.hedge_quantities,
                                        "hedge_actions": pos_size.hedge_actions,
                                        "hedge_entry_prices": hedge_prices,
                                        "entry_reason": sig.reason,
                                        "risk_scale": pos_size.risk_scale,
                                    }
                                    strategy.register_trade_entry(
                                        entry_price=sig.price,
                                        position=1 if sig.action == SignalAction.BUY else -1,
                                        stop_loss=sig.stop_loss or 0.0,
                                        target=sig.target or 0.0,
                                        risk_dist=stop_dist,
                                    )

            # EOD Square-off (strictly intraday MIS)
            if active_trade is not None:
                last_row = day_df.iloc[-1]
                last_ts = last_row["datetime"]
                last_px = float(last_row["close"])
                exit_sig = StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.instrument.symbol,
                    timestamp=last_ts,
                    price=last_px,
                    reason="SESSION_EOD_SQUAREOFF",
                    hedge_legs=dict(strategy.hedge_legs),
                )
                trade_record = self._close_pair_trade(
                    active_trade=active_trade,
                    exit_signal=exit_sig,
                    exit_timestamp=last_ts,
                    target_exit_price=last_px,
                )
                all_trades.append(trade_record)
                current_capital += trade_record["net_pnl"]
                strategy.register_trade_exit()
                active_trade = None

        return all_trades

    def _close_pair_trade(
        self,
        active_trade: Dict[str, Any],
        exit_signal: StrategySignal,
        exit_timestamp: datetime,
        target_exit_price: float,
    ) -> Dict[str, Any]:
        """
        Calculate combined P&L and friction costs across target and all hedge legs.
        """
        # 1. Target leg P&L
        target_qty = active_trade["target_quantity"]
        target_entry_px = active_trade["target_entry_price"]
        target_action = active_trade["target_action"]

        if target_action == "BUY":
            target_gross_pnl = (target_exit_price - target_entry_px) * target_qty
            target_cost = self.cost_calculator.calculate_trade_costs(
                symbol=active_trade["target_symbol"],
                instrument_type=InstrumentType.EQUITY,
                buy_price=target_entry_px,
                sell_price=target_exit_price,
                quantity=target_qty,
            ).total_cost
        else:
            target_gross_pnl = (target_entry_px - target_exit_price) * target_qty
            target_cost = self.cost_calculator.calculate_trade_costs(
                symbol=active_trade["target_symbol"],
                instrument_type=InstrumentType.EQUITY,
                buy_price=target_exit_price,
                sell_price=target_entry_px,
                quantity=target_qty,
            ).total_cost

        # 2. Hedge basket P&L & costs
        hedge_gross_pnl = 0.0
        hedge_total_costs = 0.0
        hedge_exit_prices: Dict[str, float] = {}

        for h_sym, h_qty in active_trade["hedge_quantities"].items():
            h_entry_px = active_trade["hedge_entry_prices"][h_sym]
            h_action = active_trade["hedge_actions"][h_sym]

            # Fetch exit price from peer context
            h_bar = self.peer_context.bar(h_sym, exit_timestamp)
            h_exit_px = float(h_bar["close"]) if h_bar else h_entry_px
            hedge_exit_prices[h_sym] = h_exit_px

            if h_action == "BUY":
                leg_pnl = (h_exit_px - h_entry_px) * h_qty
                leg_cost = self.cost_calculator.calculate_trade_costs(
                    symbol=h_sym,
                    instrument_type=InstrumentType.EQUITY,
                    buy_price=h_entry_px,
                    sell_price=h_exit_px,
                    quantity=h_qty,
                ).total_cost
            else:
                leg_pnl = (h_entry_px - h_exit_px) * h_qty
                leg_cost = self.cost_calculator.calculate_trade_costs(
                    symbol=h_sym,
                    instrument_type=InstrumentType.EQUITY,
                    buy_price=h_exit_px,
                    sell_price=h_entry_px,
                    quantity=h_qty,
                ).total_cost

            hedge_gross_pnl += leg_pnl
            hedge_total_costs += leg_cost

        total_gross_pnl = target_gross_pnl + hedge_gross_pnl
        total_costs = target_cost + hedge_total_costs
        net_pnl = total_gross_pnl - total_costs

        return {
            "entry_time": active_trade["entry_timestamp"],
            "exit_time": exit_timestamp,
            "symbol": active_trade["target_symbol"],
            "direction": target_action,
            "entry_price": target_entry_px,
            "exit_price": target_exit_price,
            "quantity": target_qty,
            "target_gross_pnl": round(target_gross_pnl, 2),
            "target_cost": round(target_cost, 2),
            "hedge_gross_pnl": round(hedge_gross_pnl, 2),
            "hedge_cost": round(hedge_total_costs, 2),
            "hedge_quantities": active_trade["hedge_quantities"],
            "hedge_actions": active_trade["hedge_actions"],
            "hedge_entry_prices": active_trade["hedge_entry_prices"],
            "hedge_exit_prices": hedge_exit_prices,
            "gross_pnl": round(total_gross_pnl, 2),
            "costs": round(total_costs, 2),
            "net_pnl": round(net_pnl, 2),
            "exit_reason": exit_signal.reason or "EXIT",
            "risk_scale": active_trade["risk_scale"],
        }
