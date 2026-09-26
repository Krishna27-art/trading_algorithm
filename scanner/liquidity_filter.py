"""
Liquidity Filter Layer for the 300-Stock Scanning Universe.

Evaluates raw stock market data against configurable liquidity, price, turnover,
and circuit limits before strategy execution. Handles missing stock data
gracefully with DATA_UNAVAILABLE status without crashing the scanner.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from config.settings import LiquidityFilterConfig, settings
from monitoring.logger import logger


class LiquidityStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


@dataclass
class LiquidityFilterResult:
    symbol: str
    status: LiquidityStatus
    ltp: float = 0.0
    volume: int = 0
    avg_volume_20d: int = 0
    adtv: float = 0.0
    spread_pct: float = 0.0
    rejection_reasons: List[str] = None

    def __post_init__(self):
        if self.rejection_reasons is None:
            self.rejection_reasons = []

    @property
    def is_tradable(self) -> bool:
        return self.status == LiquidityStatus.PASS


class LiquidityFilter:
    """
    Separate liquidity filter layer evaluating 300 stocks against threshold rules.
    """

    def __init__(self, config: Optional[LiquidityFilterConfig] = None):
        self.config = config or settings.liquidity_filter

    def evaluate_stock(self, stock_quote_data: Dict[str, Any]) -> LiquidityFilterResult:
        """
        Evaluates a single stock quote dictionary against liquidity criteria.
        
        Expected fields in stock_quote_data:
        - symbol: str
        - ltp / last_price: float
        - volume: int
        - avg_volume_20d: int
        - depth (optional): dict with buy/sell bid/ask
        - upper_circuit_limit / lower_circuit_limit (optional): float
        """
        symbol = stock_quote_data.get("symbol", "UNKNOWN")

        # 1. Check for missing/corrupt data
        if not stock_quote_data or stock_quote_data.get("is_data_unavailable", False):
            return LiquidityFilterResult(
                symbol=symbol,
                status=LiquidityStatus.DATA_UNAVAILABLE,
                rejection_reasons=["Data feed or historical context unavailable"],
            )

        ltp = float(stock_quote_data.get("ltp") or stock_quote_data.get("last_price") or 0.0)
        volume = int(stock_quote_data.get("volume") or 0)
        avg_vol_20d = int(stock_quote_data.get("avg_volume_20d") or volume)

        if ltp <= 0:
            return LiquidityFilterResult(
                symbol=symbol,
                status=LiquidityStatus.DATA_UNAVAILABLE,
                rejection_reasons=["Invalid or zero stock price (LTP <= 0)"],
            )

        adtv = float(avg_vol_20d * ltp)
        rejections: List[str] = []

        # 2. Minimum stock price check
        if ltp < self.config.min_stock_price:
            rejections.append(f"LTP ₹{ltp:.2f} < Min ₹{self.config.min_stock_price:.2f}")

        # 3. Minimum average volume check
        if avg_vol_20d < self.config.min_avg_volume:
            rejections.append(f"Avg Vol {avg_vol_20d:,} < Min {self.config.min_avg_volume:,}")

        # 4. Minimum ADTV (traded value) check
        if adtv < self.config.min_avg_traded_value:
            rejections.append(f"ADTV ₹{adtv:,.0f} < Min ₹{self.config.min_avg_traded_value:,.0f}")

        # 5. Bid-Ask Spread check (if market depth available)
        spread_pct = 0.0
        depth = stock_quote_data.get("depth")
        if depth and isinstance(depth, dict):
            buy_orders = depth.get("buy", [])
            sell_orders = depth.get("sell", [])
            if buy_orders and sell_orders and buy_orders[0].get("price") and sell_orders[0].get("price"):
                best_bid = float(buy_orders[0]["price"])
                best_ask = float(sell_orders[0]["price"])
                if best_bid > 0 and best_ask >= best_bid:
                    spread_pct = round(((best_ask - best_bid) / best_bid) * 100.0, 2)
                    if spread_pct > self.config.max_spread_pct:
                        rejections.append(f"Spread {spread_pct}% > Max {self.config.max_spread_pct}%")

        # 6. Upper / Lower circuit condition check
        if self.config.reject_circuits:
            upper_c = stock_quote_data.get("upper_circuit_limit")
            lower_c = stock_quote_data.get("lower_circuit_limit")
            if upper_c and ltp >= float(upper_c):
                rejections.append("Stock locked at upper circuit limit")
            if lower_c and ltp <= float(lower_c):
                rejections.append("Stock locked at lower circuit limit")

        status = LiquidityStatus.PASS if not rejections else LiquidityStatus.FAIL

        return LiquidityFilterResult(
            symbol=symbol,
            status=status,
            ltp=ltp,
            volume=volume,
            avg_volume_20d=avg_vol_20d,
            adtv=adtv,
            spread_pct=spread_pct,
            rejection_reasons=rejections,
        )

    def filter_universe(
        self, stock_quotes: List[Dict[str, Any]]
    ) -> Tuple[List[LiquidityFilterResult], List[LiquidityFilterResult]]:
        """
        Filters a list of stock quote records into (tradable_results, untradable_results).
        """
        tradable: List[LiquidityFilterResult] = []
        untradable: List[LiquidityFilterResult] = []

        for quote in stock_quotes:
            try:
                res = self.evaluate_stock(quote)
                if res.is_tradable:
                    tradable.append(res)
                else:
                    untradable.append(res)
            except Exception as e:
                sym = quote.get("symbol", "UNKNOWN")
                logger.error(f"Error evaluating liquidity for {sym}: {e}")
                untradable.append(
                    LiquidityFilterResult(
                        symbol=sym,
                        status=LiquidityStatus.DATA_UNAVAILABLE,
                        rejection_reasons=[f"Evaluation exception: {str(e)}"],
                    )
                )

        return tradable, untradable
