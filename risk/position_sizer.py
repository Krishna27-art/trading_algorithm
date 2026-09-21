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
        estimated_price: Optional[float] = None,
        enforce_max_risk_cap: bool = False,
    ) -> int:
        """
        Calculates position size in units/shares based on TRUE economic stop risk:
            Quantity = floor(Risk_Capital / Actual_Stop_Distance)
        Rounded down to contract lot size.
        Guarantees:
            Actual_Risk = Quantity * Actual_Stop_Distance <= Risk_Budget
        """
        if stop_distance <= 0 or capital <= 0:
            return 0

        # Maximum capital willing to risk on this trade (default 1%)
        risk_budget = capital * self.risk_config.risk_per_trade_pct

        # If enforce_max_risk_cap is True and stop exceeds instrument cap, reject trade
        if enforce_max_risk_cap and instrument.max_risk_cap and stop_distance > instrument.max_risk_cap:
            logger.warning(
                f"Stop distance {stop_distance:.2f} exceeds instrument maximum risk cap "
                f"{instrument.max_risk_cap:.2f}. Trade rejected."
            )
            return 0

        # Position size MUST reflect the true economic risk of the actual stop
        raw_units = risk_budget / stop_distance

        if instrument.instrument_type == InstrumentType.FUTURES:
            # Round down to nearest multiple of contract lot size
            lots = math.floor(raw_units / instrument.lot_size)
            if lots < 1:
                logger.info(
                    f"Risk budget ₹{risk_budget:,.2f} cannot afford 1 contract lot "
                    f"({instrument.lot_size} units) at {stop_distance:.2f} stop distance."
                )
                return 0
            final_quantity = lots * instrument.lot_size
        else:
            final_quantity = int(math.floor(raw_units))
            if final_quantity < 1:
                return 0

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
