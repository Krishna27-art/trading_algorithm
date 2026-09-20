"""
Statutory Indian Equity & Derivative Transaction Cost Calculator.
Adheres strictly to the October 2024 revised SEBI and Ministry of Finance schedules.
"""

from dataclasses import dataclass
from config.settings import InstrumentType, TransactionCostConfig, settings


@dataclass
class CostBreakdown:
    brokerage: float
    stt: float
    exchange_charges: float
    gst: float
    sebi_charges: float
    stamp_duty: float
    slippage_cost: float
    total_cost: float


class TransactionCostCalculator:
    def __init__(self, config: TransactionCostConfig = settings.costs):
        self.config = config

    def calculate_trade_costs(
        self,
        symbol: str,
        instrument_type: InstrumentType,
        buy_price: float,
        sell_price: float,
        quantity: int,
    ) -> CostBreakdown:
        """
        Calculates exact round-trip statutory frictions and slippage:
        - Buy turnover = buy_price * quantity
        - Sell turnover = sell_price * quantity
        - Aggregate turnover = Buy turnover + Sell turnover
        """
        buy_turnover = buy_price * quantity
        sell_turnover = sell_price * quantity
        total_turnover = buy_turnover + sell_turnover

        if instrument_type == InstrumentType.FUTURES:
            # 1. Brokerage: Flat ₹20 per executed order (Buy + Sell = ₹40)
            brokerage = self.config.futures_brokerage_per_order * 2.0

            # 2. STT: Revised Oct 2024 -> 0.02% on sell turnover
            stt = sell_turnover * self.config.futures_stt_sell_pct

            # 3. Exchange Transaction Charges: 0.00190% on aggregate turnover
            exchange_charges = total_turnover * self.config.futures_exchange_txn_pct

            # 4. SEBI Turnover Charges: ₹10 per crore (0.0001%)
            sebi_charges = total_turnover * self.config.futures_sebi_charges_pct

            # 5. GST: 18% on (Brokerage + Txn Charges + SEBI)
            gst = (brokerage + exchange_charges + sebi_charges) * self.config.futures_gst_pct

            # 6. Stamp Duty: 0.002% on buy turnover
            stamp_duty = buy_turnover * self.config.futures_stamp_duty_buy_pct

            # 7. Slippage: e.g. 0.50 index points per round-trip
            slippage_cost = self.config.futures_slippage_points * quantity

        else: # EQUITY Intraday MIS
            # Brokerage: 0.03% or ₹20 whichever lower per order
            buy_brokerage = min(buy_turnover * self.config.equity_brokerage_pct, self.config.equity_brokerage_cap)
            sell_brokerage = min(sell_turnover * self.config.equity_brokerage_pct, self.config.equity_brokerage_cap)
            brokerage = buy_brokerage + sell_brokerage

            # STT: 0.025% on sell turnover
            stt = sell_turnover * self.config.equity_stt_sell_pct

            # Exchange Charges: 0.00325% on aggregate turnover
            exchange_charges = total_turnover * self.config.equity_exchange_txn_pct

            # SEBI: ₹10 per crore
            sebi_charges = total_turnover * self.config.equity_sebi_charges_pct

            # GST: 18% on (Brokerage + Txn + SEBI)
            gst = (brokerage + exchange_charges + sebi_charges) * self.config.equity_gst_pct

            # Stamp Duty: 0.003% on buy turnover
            stamp_duty = buy_turnover * self.config.equity_stamp_duty_buy_pct

            # Slippage
            slippage_cost = total_turnover * self.config.equity_slippage_pct

        total_cost = round(
            brokerage + stt + exchange_charges + gst + sebi_charges + stamp_duty + slippage_cost,
            2,
        )

        return CostBreakdown(
            brokerage=round(brokerage, 2),
            stt=round(stt, 2),
            exchange_charges=round(exchange_charges, 2),
            gst=round(gst, 2),
            sebi_charges=round(sebi_charges, 2),
            stamp_duty=round(stamp_duty, 2),
            slippage_cost=round(slippage_cost, 2),
            total_cost=total_cost,
        )
