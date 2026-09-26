"""
Statutory SEBI & NSE Cost Calculators for Delivery & Index Options.
"""

from typing import Dict, Any
from config.settings import DeliveryCostConfig, IndexOptionsCostConfig, settings


class DeliveryCostCalculator:
    def __init__(self, config: DeliveryCostConfig = settings.delivery_costs):
        self.cfg = config

    def calculate_turnover_costs(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
    ) -> Dict[str, float]:
        turnover = quantity * price
        brokerage = max(0.0, self.cfg.brokerage_per_order)
        stt = turnover * (self.cfg.stt_buy_pct if side.upper() == "BUY" else self.cfg.stt_sell_pct)
        exchange_txn = turnover * self.cfg.exchange_txn_pct
        sebi = turnover * self.cfg.sebi_charges_pct
        stamp_duty = turnover * self.cfg.stamp_duty_buy_pct if side.upper() == "BUY" else 0.0
        gst = (brokerage + exchange_txn + sebi) * self.cfg.gst_pct
        slippage = turnover * self.cfg.slippage_pct
        dp_charges = self.cfg.dp_charges_per_sell_scrip if side.upper() == "SELL" else 0.0

        total = brokerage + stt + exchange_txn + sebi + stamp_duty + gst + slippage + dp_charges
        return {
            "brokerage": brokerage,
            "stt": stt,
            "exchange_txn": exchange_txn,
            "sebi": sebi,
            "stamp_duty": stamp_duty,
            "gst": gst,
            "slippage": slippage,
            "dp_charges": dp_charges,
            "total_frictions": total,
        }


class IndexOptionsCostCalculator:
    def __init__(self, config: IndexOptionsCostConfig = settings.options_costs):
        self.cfg = config

    def calculate_option_costs(
        self,
        side: str,
        quantity: int,
        premium: float,
    ) -> Dict[str, float]:
        premium_turnover = quantity * premium
        brokerage = self.cfg.brokerage_per_order
        stt = premium_turnover * self.cfg.stt_sell_premium_pct if side.upper() == "SELL" else 0.0
        exchange_txn = premium_turnover * self.cfg.exchange_txn_premium_pct
        sebi = premium_turnover * self.cfg.sebi_charges_pct
        stamp_duty = premium_turnover * self.cfg.stamp_duty_buy_pct if side.upper() == "BUY" else 0.0
        gst = (brokerage + exchange_txn + sebi) * self.cfg.gst_pct
        slippage = premium_turnover * self.cfg.slippage_premium_pct

        total = brokerage + stt + exchange_txn + sebi + stamp_duty + gst + slippage
        return {
            "brokerage": brokerage,
            "stt": stt,
            "exchange_txn": exchange_txn,
            "sebi": sebi,
            "stamp_duty": stamp_duty,
            "gst": gst,
            "slippage": slippage,
            "total_frictions": total,
        }
