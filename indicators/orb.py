"""
Opening Range Breakout (ORB) Calculator & Volatility Filters.
Defines structural opening range between 09:15 and 09:45 IST (First two 15-min bars).
"""

from dataclasses import dataclass
from datetime import time
from typing import Optional
import pandas as pd


@dataclass
class OpeningRange:
    high: float
    low: float
    width: float
    is_valid_volatility: bool
    is_established: bool = True


class ORBCalculator:
    @staticmethod
    def calculate_opening_range(
        day_15m_bars: pd.DataFrame,
        min_orb_range: float = 40.0,
        max_orb_range: float = 120.0,
        atr_14_daily: Optional[float] = None,
        is_equity: bool = False,
    ) -> Optional[OpeningRange]:
        """
        Extracts high, low, and width between 09:15 and 09:44:59 IST.
        Validates volatility cutoff:
        - For Nifty: OR_width >= 40 points
        - For Equities: OR_width >= 0.85 * (ATR_14 / sqrt(75)) * 10
        """
        if day_15m_bars.empty:
            return None

        bars = day_15m_bars.copy()
        if "datetime" in bars.columns and not isinstance(bars.index, pd.DatetimeIndex):
            bars.set_index("datetime", inplace=True)

        # First two 15-minute completed bars (covers 09:15-09:45 IST)
        orb_bars = bars.between_time("09:15", "09:45")
        if len(orb_bars) < 2:
            return None
        orb_bars = orb_bars.iloc[:2]

        or_high = float(orb_bars["high"].max())
        or_low = float(orb_bars["low"].min())
        or_width = round(or_high - or_low, 2)

        # Volatility Filter Evaluation
        if is_equity and atr_14_daily is not None:
            threshold = 0.85 * (atr_14_daily / (75 ** 0.5)) * 10.0
            is_valid = or_width >= threshold
        else:
            is_valid = or_width >= min_orb_range

        return OpeningRange(
            high=or_high,
            low=or_low,
            width=or_width,
            is_valid_volatility=is_valid,
            is_established=True,
        )
