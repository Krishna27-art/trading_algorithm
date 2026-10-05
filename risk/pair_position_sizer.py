"""
Pair Position Sizer for Cross-Sectional Relative Value (CRSD) Strategies.

Calculates multi-leg position quantities under a single unified pair risk budget:
    Total_Risk_Budget = Capital * risk_per_trade_pct * risk_scale
    Target_Quantity = floor(Total_Risk_Budget / Target_Stop_Distance)
    Hedge_Leg_Quantity_k = round(Target_Notional * |w_k| / Hedge_Price_k)

Ensures all legs open and size together under one global risk cap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Dict, Optional

from config.settings import RiskConfig, settings
from monitoring.logger import logger


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
    """
    Allocates position sizes across primary and hedge legs of a statistical arbitrage pair/basket.
    """

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
        margin_requirement_pct: float = 0.20,  # 20% standard intraday MIS equity margin
    ) -> PairPositionSize:
        """
        Compute coordinated quantities for target leg and all basket hedge legs.
        """
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

        # Unified single pair risk budget consuming risk_scale
        scale = max(0.1, min(float(risk_scale), 1.0))
        risk_budget = capital * self.risk_config.risk_per_trade_pct * scale

        # Target leg sizing
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

        # Hedge leg sizing proportional to signed notional weights
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
                qty_k = 1  # Minimum 1 share to maintain hedge presence

            # Signed weight convention: positive weight -> BUY, negative weight -> SELL
            action_k = "BUY" if weight > 0 else "SELL"
            hedge_quantities[clean_sym] = qty_k
            hedge_actions[clean_sym] = action_k
            hedge_notionals[clean_sym] = qty_k * px

        # Margin constraint check
        if available_margin is not None and self.risk_config.enforce_margin_check:
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
