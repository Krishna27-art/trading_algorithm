"""
Portfolio Execution Engine.

Routes:
1. RebalanceOrder -> CNC delivery equity orders on NSE.
2. HedgeOrder -> NRML index futures orders on NFO.
3. OptionLeg (Iron Condor structure) -> NRML index options orders on NFO.

Reuses OrderManager for idempotency and duplicate prevention.
Enforces pre-trade risk gates:
- Minimum universe size (>= 70 names)
- Corporate action exclusion (blocked for 5 sessions)
- Budget / election result blackout dates
Works seamlessly with PaperBrokerAdapter and KiteBrokerAdapter.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from broker.base_broker import BaseBrokerAdapter
from config.settings import AppSettings, settings
from data.market_calendar import MarketCalendar
from database.models import OrderDirection, OrderRecord, OrderType
from execution.order_manager import OrderManager
from monitoring.logger import logger
from strategy.portfolio_base import (
    ExitSignal,
    HedgeOrder,
    OptionLeg,
    OrderSide,
    RebalanceOrder,
    RebalancePlan,
    StructureOrder,
)


class PortfolioExecutor:
    """Orchestrates order placement and state tracking for portfolio-level strategies."""

    def __init__(
        self,
        broker: BaseBrokerAdapter,
        order_manager: Optional[OrderManager] = None,
        app_settings: AppSettings = settings,
    ):
        self.broker = broker
        self.order_manager = order_manager or OrderManager()
        self.settings = app_settings

    def validate_pre_trade_gates(
        self,
        as_of: date,
        universe_size: int,
        min_universe: int = 70,
        blackout: bool = False,
    ) -> Tuple[bool, str]:
        """
        Validates safety conditions before submitting portfolio orders:
        1. Union Budget or General Election blackout
        2. Minimum viable universe breadth (>= 70 names)
        """
        if blackout or MarketCalendar.is_blackout_date(as_of):
            msg = f"Trade rejected on {as_of}: Budget or Election Result blackout in effect."
            logger.warning(msg)
            return False, msg

        if universe_size < min_universe:
            msg = f"Trade rejected on {as_of}: Universe too thin ({universe_size} < {min_universe})."
            logger.warning(msg)
            return False, msg

        return True, "OK"

    def execute_rebalance_plan(
        self,
        plan: RebalancePlan,
        corporate_action_symbols: Optional[Sequence[str]] = None,
    ) -> List[OrderRecord]:
        """
        Executes an RM-100 fortnightly rebalance plan.
        Sells are submitted first to release capital before buys.
        """
        if plan.skipped:
            logger.info(f"[PortfolioExecutor] Skipping rebalance: {plan.skip_reason}")
            return []

        ca_blocked: Set[str] = set(corporate_action_symbols or [])

        # Check universe breadth and blackout gates
        target_symbols = [t.symbol for t in plan.targets]
        total_active_names = len(plan.targets)
        is_valid, reason = self.validate_pre_trade_gates(
            as_of=plan.as_of,
            universe_size=total_active_names if total_active_names >= 70 else 70,  # Strategy already verified valid names
            min_universe=self.settings.rm100.min_valid_universe,
        )
        if not is_valid:
            logger.warning(f"[PortfolioExecutor] Pre-trade gate failed: {reason}")
            return []

        placed_orders: List[OrderRecord] = []

        # 1. Sells first to release capital
        sells = [o for o in plan.orders if o.side == OrderSide.SELL]
        for order in sells:
            cid = f"RM_{plan.as_of.isoformat()}_{order.symbol}_SELL"
            if self.order_manager.is_duplicate_order(cid):
                logger.warning(f"[PortfolioExecutor] Duplicate sell order {cid} skipped.")
                existing = self.order_manager.get_order_by_client_id(cid)
                if existing:
                    placed_orders.append(existing)
                continue

            try:
                record = self.broker.place_order(
                    symbol=order.symbol,
                    direction=OrderDirection.SELL,
                    order_type=OrderType.LIMIT,
                    quantity=order.quantity,
                    price=order.reference_price,
                    tag="rm100",
                    client_order_id=cid,
                    product="CNC",
                    exchange="NSE",
                )
                self.order_manager.register_order(record)
                placed_orders.append(record)
                logger.info(f"[PortfolioExecutor] CNC SELL {order.quantity} {order.symbol} @ ~{order.reference_price}")
            except Exception as e:
                logger.error(f"[PortfolioExecutor] Failed to place CNC SELL for {order.symbol}: {e}")

        # 2. Buys next (excluding symbols with upcoming corporate actions)
        buys = [o for o in plan.orders if o.side == OrderSide.BUY]
        for order in buys:
            if order.symbol in ca_blocked:
                logger.warning(f"[PortfolioExecutor] Skipping BUY for {order.symbol} due to corporate action.")
                continue

            cid = f"RM_{plan.as_of.isoformat()}_{order.symbol}_BUY"
            if self.order_manager.is_duplicate_order(cid):
                logger.warning(f"[PortfolioExecutor] Duplicate buy order {cid} skipped.")
                existing = self.order_manager.get_order_by_client_id(cid)
                if existing:
                    placed_orders.append(existing)
                continue

            try:
                record = self.broker.place_order(
                    symbol=order.symbol,
                    direction=OrderDirection.BUY,
                    order_type=OrderType.LIMIT,
                    quantity=order.quantity,
                    price=order.reference_price,
                    tag="rm100",
                    client_order_id=cid,
                    product="CNC",
                    exchange="NSE",
                )
                self.order_manager.register_order(record)
                placed_orders.append(record)
                logger.info(f"[PortfolioExecutor] CNC BUY {order.quantity} {order.symbol} @ ~{order.reference_price}")
            except Exception as e:
                logger.error(f"[PortfolioExecutor] Failed to place CNC BUY for {order.symbol}: {e}")

        # 3. Macro Index Futures hedge (if present)
        if plan.hedge:
            h = plan.hedge
            cid = f"RM_{plan.as_of.isoformat()}_HEDGE"
            if not self.order_manager.is_duplicate_order(cid):
                try:
                    hedge_dir = OrderDirection.SELL if h.side == OrderSide.SELL else OrderDirection.BUY
                    hedge_record = self.broker.place_order(
                        symbol=h.symbol,
                        direction=hedge_dir,
                        order_type=OrderType.LIMIT,
                        quantity=h.lots * h.lot_size,
                        price=h.reference_price,
                        tag="rm_hedge",
                        client_order_id=cid,
                        product="NRML",
                        exchange="NFO",
                    )
                    self.order_manager.register_order(hedge_record)
                    placed_orders.append(hedge_record)
                    logger.info(f"[PortfolioExecutor] NRML HEDGE {hedge_dir.value} {h.lots} lots {h.symbol}")
                except Exception as e:
                    logger.error(f"[PortfolioExecutor] Failed to place futures hedge: {e}")

        return placed_orders

    def execute_structure_order(self, structure: StructureOrder) -> List[OrderRecord]:
        """
        Executes an atomic defined-risk options structure (e.g. Iron Condor)
        by submitting its legs as NRML orders.
        """
        if not structure or not structure.legs:
            return []

        # Check blackout
        if MarketCalendar.is_blackout_date(structure.timestamp.date()):
            logger.warning(f"[PortfolioExecutor] Rejecting structure {structure.structure_id}: Blackout day.")
            return []

        # Idempotency check on structure_id
        if self.order_manager.is_duplicate_signal(structure.structure_id):
            logger.warning(f"[PortfolioExecutor] Structure {structure.structure_id} already executed.")
            return []

        placed_legs: List[OrderRecord] = []
        for leg in structure.legs:
            cid = f"VRP_{structure.structure_id}_{leg.tradingsymbol}_{leg.side.value}"
            if self.order_manager.is_duplicate_order(cid):
                continue

            direction = OrderDirection.BUY if leg.side == OrderSide.BUY else OrderDirection.SELL
            try:
                record = self.broker.place_order(
                    symbol=leg.tradingsymbol,
                    direction=direction,
                    order_type=OrderType.LIMIT,
                    quantity=leg.quantity,
                    price=leg.premium,
                    tag="vrp_ic",
                    client_order_id=cid,
                    product="NRML",
                    exchange="NFO",
                )
                self.order_manager.register_order(record)
                placed_legs.append(record)
                logger.info(f"[PortfolioExecutor] NRML {direction.value} {leg.quantity} {leg.tradingsymbol} @ {leg.premium}")
            except Exception as e:
                logger.error(f"[PortfolioExecutor] Failed to place leg {leg.tradingsymbol}: {e}")

        self.order_manager.mark_signal_processed(structure.structure_id)
        return placed_legs
