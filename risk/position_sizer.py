"""
Fixed Fractional Position Sizer with regulatory peak-margin safeguards.
Formula:
    Risk_Capital = Capital * 0.01 (1%)
    R_trade = min(Stop_Distance, 80) if OR_Width > 120 else Stop_Distance
    Quantity = floor(Risk_Capital / R_trade)
    Rounded down to contract lot size.
"""

import math
from typing import Optional
from config.settings import InstrumentConfig, InstrumentType, RiskConfig, settings
from monitoring.logger import logger


class PositionSizer:
    def __init__(self, risk_config: RiskConfig = settings.risk):
        self.risk_config = risk_config

    def calculate_order_quantity(
        self,
        capital: float,
        stop_distance: float,
        instrument: InstrumentConfig,
        or_width: Optional[float] = None,
        available_margin: Optional[float] = None,
        estimated_price: float = 24000.0,
    ) -> int:
        """
        Calculates position size in units/shares.
        Respects:
        - 1% account risk fraction
        - 80-point maximum risk cap when OR_width > 120 points
        - Contract lot size rounding (e.g. multiples of 25 for Nifty futures)
        - Available margin capacity
        """
        if stop_distance <= 0 or capital <= 0:
            return 0

        # Maximum capital willing to risk on this trade (default 1%)
        risk_budget = capital * self.risk_config.risk_per_trade_pct

        # Apply research rule: If OR_width > 120 pts, cap effective risk distance at 80 pts
        effective_risk_distance = stop_distance
        if or_width is not None and or_width > instrument.max_orb_range:
            effective_risk_distance = min(stop_distance, instrument.max_risk_cap)

        raw_units = risk_budget / effective_risk_distance

        if instrument.instrument_type == InstrumentType.FUTURES:
            # Round down to nearest multiple of contract lot size
            lots = math.floor(raw_units / instrument.lot_size)
            if lots < 1:
                # If 1% risk cannot even afford 1 lot, return 0 (or 1 lot if capital allows with strict warning)
                logger.warning(
                    f"Capital ₹{capital:,.2f} with 1% risk cannot cover 1 contract lot ({instrument.lot_size} units). "
                    f"Required risk: ₹{effective_risk_distance * instrument.lot_size:,.2f} vs Budget: ₹{risk_budget:,.2f}."
                )
                # For realistic simulation, if risk budget is close or user wants minimum 1 lot:
                return 0
            final_quantity = lots * instrument.lot_size
        else:
            final_quantity = max(int(raw_units), 1)

        # Margin sanity check (assuming ~12% MIS intraday margin for index futures or 20% for equities)
        if available_margin is not None and self.risk_config.enforce_margin_check:
            margin_per_unit = estimated_price * (0.12 if instrument.instrument_type == InstrumentType.FUTURES else 0.20)
            required_margin = final_quantity * margin_per_unit
            if required_margin > available_margin:
                max_allowed_units = int(available_margin / margin_per_unit)
                if instrument.instrument_type == InstrumentType.FUTURES:
                    max_allowed_units = math.floor(max_allowed_units / instrument.lot_size) * instrument.lot_size
                final_quantity = max(max_allowed_units, 0)
                logger.warning(f"Position size clamped to {final_quantity} due to available margin limits.")

        return final_quantity
