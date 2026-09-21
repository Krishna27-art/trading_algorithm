"""
Transaction costs for CNC equity delivery and NIFTY index options.

risk/transaction_costs.py covers FUTURES and equity INTRADAY (MIS) only.
Neither new engine trades either of those legs:

  * NSE-RM-100 buys delivery (CNC). Delivery STT is 0.10% on BOTH sides
    (0.20% round trip) versus 0.025% sell-side-only for MIS -- an 8x
    difference on the buy leg that a backtest must not paper over.
  * NSE-VRP-INDEX trades index options, where STT is 0.10% on the PREMIUM
    of the sell leg (revised up from 0.0625% on 1 Oct 2024) and the
    exchange charge is 0.03503% of premium -- an order of magnitude above
    the futures rate, and charged on premium, not notional.

All rates below are the post-1-October-2024 schedule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config.settings import DeliveryCostConfig, IndexOptionsCostConfig, settings


@dataclass
class CostLine:
    brokerage: float
    stt: float
    exchange_charges: float
    sebi_charges: float
    stamp_duty: float
    gst: float
    slippage_cost: float
    dp_charges: float
    total_cost: float


class DeliveryCostCalculator:
    """Round-trip cost of one CNC equity position."""

    def __init__(self, config: Optional[DeliveryCostConfig] = None):
        if config is not None:
            self.config = config
        else:
            try:
                from config.settings import settings
                self.config = settings.delivery_costs
            except Exception:
                self.config = DeliveryCostConfig()

    def calculate(self, buy_price: float, sell_price: float, quantity: int) -> CostLine:
        c = self.config
        buy_turnover = buy_price * quantity
        sell_turnover = sell_price * quantity
        total_turnover = buy_turnover + sell_turnover

        brokerage = c.brokerage_per_order * 2.0
        stt = buy_turnover * c.stt_buy_pct + sell_turnover * c.stt_sell_pct
        exchange_charges = total_turnover * c.exchange_txn_pct
        sebi_charges = total_turnover * c.sebi_charges_pct
        stamp_duty = buy_turnover * c.stamp_duty_buy_pct
        gst = (brokerage + exchange_charges + sebi_charges) * c.gst_pct
        slippage_cost = total_turnover * c.slippage_pct
        dp_charges = c.dp_charges_per_sell_scrip  # levied once, on the sell

        total = brokerage + stt + exchange_charges + sebi_charges + stamp_duty + gst + slippage_cost + dp_charges
        return CostLine(
            brokerage=round(brokerage, 2),
            stt=round(stt, 2),
            exchange_charges=round(exchange_charges, 2),
            sebi_charges=round(sebi_charges, 2),
            stamp_duty=round(stamp_duty, 2),
            gst=round(gst, 2),
            slippage_cost=round(slippage_cost, 2),
            dp_charges=round(dp_charges, 2),
            total_cost=round(total, 2),
        )


class IndexOptionsCostCalculator:
    """Cost of a multi-leg index options structure, entry + exit.

    Legs are priced on PREMIUM turnover, not notional -- getting that wrong
    inflates modelled costs by roughly two orders of magnitude and will make
    any options backtest look unprofitable for the wrong reason.
    """

    def __init__(self, config: Optional[IndexOptionsCostConfig] = None):
        if config is not None:
            self.config = config
        else:
            try:
                from config.settings import settings
                self.config = settings.options_costs
            except Exception:
                self.config = IndexOptionsCostConfig()

    def calculate_leg(
        self, entry_premium: float, exit_premium: float, quantity: int, is_short: bool
    ) -> CostLine:
        c = self.config
        entry_turnover = entry_premium * quantity
        exit_turnover = exit_premium * quantity
        total_turnover = entry_turnover + exit_turnover

        brokerage = c.brokerage_per_order * 2.0
        # STT applies to the sell transaction: the entry for a short leg,
        # the exit for a long leg.
        sell_turnover = entry_turnover if is_short else exit_turnover
        buy_turnover = exit_turnover if is_short else entry_turnover

        stt = sell_turnover * c.stt_sell_premium_pct
        exchange_charges = total_turnover * c.exchange_txn_premium_pct
        sebi_charges = total_turnover * c.sebi_charges_pct
        stamp_duty = buy_turnover * c.stamp_duty_buy_pct
        gst = (brokerage + exchange_charges + sebi_charges) * c.gst_pct
        slippage_cost = total_turnover * c.slippage_premium_pct

        total = brokerage + stt + exchange_charges + sebi_charges + stamp_duty + gst + slippage_cost
        return CostLine(
            brokerage=round(brokerage, 2),
            stt=round(stt, 2),
            exchange_charges=round(exchange_charges, 2),
            sebi_charges=round(sebi_charges, 2),
            stamp_duty=round(stamp_duty, 2),
            gst=round(gst, 2),
            slippage_cost=round(slippage_cost, 2),
            dp_charges=0.0,
            total_cost=round(total, 2),
        )

    def calculate_structure(self, legs: list, exit_premiums: dict) -> CostLine:
        """legs: list[OptionLeg]; exit_premiums: {tradingsymbol: price}."""
        agg = CostLine(0, 0, 0, 0, 0, 0, 0, 0, 0)
        for leg in legs:
            line = self.calculate_leg(
                entry_premium=leg.premium,
                exit_premium=float(exit_premiums.get(leg.tradingsymbol, 0.0)),
                quantity=leg.quantity,
                is_short=(str(leg.side.value if hasattr(leg.side, "value") else leg.side) == "SELL"),
            )
            agg = CostLine(
                brokerage=agg.brokerage + line.brokerage,
                stt=agg.stt + line.stt,
                exchange_charges=agg.exchange_charges + line.exchange_charges,
                sebi_charges=agg.sebi_charges + line.sebi_charges,
                stamp_duty=agg.stamp_duty + line.stamp_duty,
                gst=agg.gst + line.gst,
                slippage_cost=agg.slippage_cost + line.slippage_cost,
                dp_charges=0.0,
                total_cost=agg.total_cost + line.total_cost,
            )
        return CostLine(**{k: round(v, 2) for k, v in agg.__dict__.items()})
