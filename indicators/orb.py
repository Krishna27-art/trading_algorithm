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
    min_required: float = 0.0
    max_allowed: float = float("inf")


class ORBCalculator:
    @staticmethod
    def calculate_opening_range(
        day_15m_bars: pd.DataFrame,
        min_orb_range: float = 40.0,
        max_orb_range: float = 120.0,
        atr_14_daily: Optional[float] = None,
        is_equity: bool = False,
        equity_min_range_pct: Optional[float] = None,
        equity_max_range_pct: Optional[float] = None,
        timestamps_are_candle_open: bool = True,
    ) -> Optional[OpeningRange]:
        """
        Extracts high, low, and width between 09:15 and 09:44:59 IST.
        Validates volatility cutoff:
        - For Nifty/Futures: OR_width >= min_orb_range
        - For Equities: percentage of reference price or min_orb_range/ATR-based cutoff.
        """
        if day_15m_bars.empty:
            return None

        bars = day_15m_bars.copy()
        if "datetime" in bars.columns and not isinstance(bars.index, pd.DatetimeIndex):
            bars.set_index("datetime", inplace=True)
        if not isinstance(bars.index, pd.DatetimeIndex):
            try:
                bars.index = pd.to_datetime(bars.index)
            except Exception:
                pass

        if timestamps_are_candle_open:
            orb_bars = bars.between_time("09:15", "09:30")
            expected_times = {"09:15", "09:30"}
        else:
            orb_bars = bars.between_time("09:30", "09:45")
            expected_times = {"09:30", "09:45"}

        if len(orb_bars) < 2:
            return None

        actual_times = {
            ts.strftime("%H:%M")
            for ts in orb_bars.index
        }

        if not expected_times.issubset(actual_times):
            return None

        orb_bars = orb_bars.loc[
            orb_bars.index.strftime("%H:%M").isin(expected_times)
        ].sort_index().iloc[:2]

        or_high = float(orb_bars["high"].max())
        or_low = float(orb_bars["low"].min())
        or_width = round(or_high - or_low, 2)

        # Volatility Filter Evaluation
        if is_equity:
            ref_price = float(orb_bars["open"].iloc[0]) if "open" in orb_bars.columns else (or_high + or_low) / 2.0
            if atr_14_daily is not None:
                min_req = 0.85 * (atr_14_daily / (75 ** 0.5)) * 10.0
                max_allow = max_orb_range
            elif equity_min_range_pct is not None and equity_max_range_pct is not None:
                min_req = max(round(ref_price * equity_min_range_pct, 2), 0.01)
                max_allow = max(round(ref_price * equity_max_range_pct, 2), min_req)
            else:
                min_req = float(min_orb_range)
                max_allow = float(max_orb_range)
            is_valid = (or_width >= min_req) and (or_width <= max_allow)
        else:
            min_req = float(min_orb_range)
            max_allow = float(max_orb_range)
            is_valid = or_width >= min_req

        return OpeningRange(
            high=or_high,
            low=or_low,
            width=or_width,
            is_valid_volatility=is_valid,
            is_established=True,
            min_required=min_req,
            max_allowed=max_allow,
        )
