from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from backend.config.settings import LiquidityFilterConfig, settings
from backend.monitoring.logger import logger


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
    spread_pct: Optional[float] = None
    rejection_reasons: List[str] = field(default_factory=list)

    @property
    def is_tradable(self) -> bool:
        return self.status == LiquidityStatus.PASS


def _finite_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


class LiquidityFilter:
    def __init__(self, config: Optional[LiquidityFilterConfig] = None):
        self.config = config or settings.liquidity_filter
        for name in (
            "min_stock_price",
            "min_avg_volume",
            "min_avg_traded_value",
            "max_spread_pct",
        ):
            if _finite_float(getattr(self.config, name, None)) is None:
                raise ValueError(f"liquidity filter config '{name}' must be a finite number")

    @staticmethod
    def _unavailable(
        symbol: str,
        reason: str,
        ltp: float = 0.0,
        volume: int = 0,
        avg_volume_20d: int = 0,
        adtv: float = 0.0,
    ) -> LiquidityFilterResult:
        return LiquidityFilterResult(
            symbol=symbol,
            status=LiquidityStatus.DATA_UNAVAILABLE,
            ltp=ltp,
            volume=volume,
            avg_volume_20d=avg_volume_20d,
            adtv=adtv,
            rejection_reasons=[reason],
        )

    @staticmethod
    def _best_prices(depth: Any) -> Optional[Tuple[float, float]]:
        if not isinstance(depth, dict):
            return None
        buy_orders = depth.get("buy")
        sell_orders = depth.get("sell")
        if not isinstance(buy_orders, (list, tuple)) or not buy_orders:
            return None
        if not isinstance(sell_orders, (list, tuple)) or not sell_orders:
            return None
        if not isinstance(buy_orders[0], dict) or not isinstance(sell_orders[0], dict):
            return None
        best_bid = _finite_float(buy_orders[0].get("price"))
        best_ask = _finite_float(sell_orders[0].get("price"))
        if best_bid is None or best_ask is None:
            return None
        return best_bid, best_ask

    def evaluate_stock(self, stock_quote_data: Dict[str, Any]) -> LiquidityFilterResult:
        if not isinstance(stock_quote_data, dict) or not stock_quote_data:
            return self._unavailable(
                "UNKNOWN",
                "Data feed or historical context unavailable",
            )

        symbol = str(stock_quote_data.get("symbol") or "UNKNOWN")

        if stock_quote_data.get("is_data_unavailable", False):
            return self._unavailable(
                symbol,
                "Data feed or historical context unavailable",
            )

        raw_ltp = stock_quote_data.get("ltp")
        if raw_ltp is None:
            raw_ltp = stock_quote_data.get("last_price")
        ltp = _finite_float(raw_ltp)
        if ltp is None or ltp <= 0:
            return self._unavailable(
                symbol,
                "LTP missing, non-finite or not positive",
            )

        raw_volume = stock_quote_data.get("volume")
        if raw_volume is None:
            volume = 0
        else:
            volume_value = _finite_float(raw_volume)
            if volume_value is None or volume_value < 0:
                return self._unavailable(
                    symbol,
                    "Volume non-finite or negative",
                    ltp=ltp,
                )
            volume = int(volume_value)

        avg_value = _finite_float(stock_quote_data.get("avg_volume_20d"))
        if avg_value is None or avg_value <= 0:
            return self._unavailable(
                symbol,
                "20-day full-session average volume unavailable, invalid or non-finite",
                ltp=ltp,
                volume=volume,
            )
        avg_vol_20d = int(avg_value)
        if avg_vol_20d <= 0:
            return self._unavailable(
                symbol,
                "20-day full-session average volume unavailable, invalid or non-finite",
                ltp=ltp,
                volume=volume,
            )

        adtv = float(avg_vol_20d * ltp)
        if math.isnan(adtv) or math.isinf(adtv):
            return self._unavailable(
                symbol,
                "ADTV non-finite",
                ltp=ltp,
                volume=volume,
                avg_volume_20d=avg_vol_20d,
            )

        depth_data = stock_quote_data.get("depth")
        best_bid: Optional[float] = None
        best_ask: Optional[float] = None
        if depth_data is not None:
            best_prices = self._best_prices(depth_data)
            if best_prices is None:
                return self._unavailable(
                    symbol,
                    "Required live market depth missing, incomplete or non-finite; spread cannot be validated",
                    ltp=ltp,
                    volume=volume,
                    avg_volume_20d=avg_vol_20d,
                    adtv=adtv,
                )
            best_bid, best_ask = best_prices

        upper_limit: Optional[float] = None
        lower_limit: Optional[float] = None
        if self.config.reject_circuits:
            raw_upper = stock_quote_data.get("upper_circuit_limit")
            raw_lower = stock_quote_data.get("lower_circuit_limit")
            if raw_upper is not None:
                upper_limit = _finite_float(raw_upper)
                if upper_limit is None:
                    return self._unavailable(
                        symbol,
                        "Upper circuit limit non-finite or invalid",
                        ltp=ltp,
                        volume=volume,
                        avg_volume_20d=avg_vol_20d,
                        adtv=adtv,
                    )
            if raw_lower is not None:
                lower_limit = _finite_float(raw_lower)
                if lower_limit is None:
                    return self._unavailable(
                        symbol,
                        "Lower circuit limit non-finite or invalid",
                        ltp=ltp,
                        volume=volume,
                        avg_volume_20d=avg_vol_20d,
                        adtv=adtv,
                    )

        rejections: List[str] = []

        if ltp < self.config.min_stock_price:
            rejections.append(f"LTP ₹{ltp:.2f} < Min ₹{self.config.min_stock_price:.2f}")

        if avg_vol_20d < self.config.min_avg_volume:
            rejections.append(f"Avg Vol {avg_vol_20d:,} < Min {self.config.min_avg_volume:,}")

        if adtv < self.config.min_avg_traded_value:
            rejections.append(f"ADTV ₹{adtv:,.0f} < Min ₹{self.config.min_avg_traded_value:,.0f}")

        spread_pct: Optional[float] = None
        if best_bid is not None and best_ask is not None:
            if best_bid <= 0 or best_ask <= best_bid:
                rejections.append("Invalid live bid/ask spread.")
            else:
                spread_pct = round(((best_ask - best_bid) / best_bid) * 100.0, 2)
                if spread_pct > self.config.max_spread_pct:
                    rejections.append(f"Spread {spread_pct}% > Max {self.config.max_spread_pct}%")

        if upper_limit and ltp >= upper_limit:
            rejections.append("Stock locked at upper circuit limit")
        if lower_limit and ltp <= lower_limit:
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
                sym = quote.get("symbol", "UNKNOWN") if isinstance(quote, dict) else "UNKNOWN"
                logger.error(f"Error evaluating liquidity for {sym}: {e}")
                untradable.append(
                    self._unavailable(str(sym), f"Evaluation exception: {str(e)}")
                )

        return tradable, untradable