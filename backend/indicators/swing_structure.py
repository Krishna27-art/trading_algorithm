from dataclasses import dataclass
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd


@dataclass
class SwingPoint:
    swing_type: str
    price: float
    timestamp: datetime
    bar_index: int


@dataclass
class MarketStructureFacts:
    pdh: Optional[float] = None
    pdl: Optional[float] = None
    pdc: Optional[float] = None
    pdo: Optional[float] = None
    last_swing_high: Optional[float] = None
    last_swing_low: Optional[float] = None
    swing_high_timestamp: Optional[datetime] = None
    swing_low_timestamp: Optional[datetime] = None
    distance_to_pdh_pct: Optional[float] = None
    distance_to_pdl_pct: Optional[float] = None


class SwingStructureEngine:
    def __init__(self, pivot_left: int = 2, pivot_right: int = 2):
        self.pivot_left = pivot_left
        self.pivot_right = pivot_right

    def extract_previous_day_levels(self, df: pd.DataFrame) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        if df.empty or "datetime" not in df.columns:
            return None, None, None, None
        df_copy = df.copy()
        df_copy["datetime"] = pd.to_datetime(df_copy["datetime"])
        df_copy["date"] = df_copy["datetime"].dt.date
        unique_dates = df_copy["date"].unique()
        if len(unique_dates) < 2:
            return None, None, None, None
        prev_date = unique_dates[-2]
        prev_df = df_copy[df_copy["date"] == prev_date]
        if prev_df.empty:
            return None, None, None, None
        pdh = float(prev_df["high"].max())
        pdl = float(prev_df["low"].min())
        pdo = float(prev_df["open"].iloc[0])
        pdc = float(prev_df["close"].iloc[-1])
        return pdh, pdl, pdc, pdo

    def detect_swings(self, df: pd.DataFrame) -> List[SwingPoint]:
        swings: List[SwingPoint] = []
        if df.empty or len(df) < (self.pivot_left + self.pivot_right + 1):
            return swings
        highs = df["high"].values
        lows = df["low"].values
        timestamps = pd.to_datetime(df["datetime"]).values
        n = len(df)
        for i in range(self.pivot_left, n - self.pivot_right):
            current_high = highs[i]
            left_highs = highs[i - self.pivot_left:i]
            right_highs = highs[i + 1:i + 1 + self.pivot_right]
            if all(current_high >= h for h in left_highs) and all(current_high > h for h in right_highs):
                ts = pd.Timestamp(timestamps[i]).to_pydatetime()
                swings.append(SwingPoint(swing_type="HIGH", price=float(current_high), timestamp=ts, bar_index=i))

            current_low = lows[i]
            left_lows = lows[i - self.pivot_left:i]
            right_lows = lows[i + 1:i + 1 + self.pivot_right]
            if all(current_low <= l for l in left_lows) and all(current_low < l for l in right_lows):
                ts = pd.Timestamp(timestamps[i]).to_pydatetime()
                swings.append(SwingPoint(swing_type="LOW", price=float(current_low), timestamp=ts, bar_index=i))
        return swings

    def calculate_facts(self, df: pd.DataFrame, current_ltp: Optional[float] = None) -> MarketStructureFacts:
        pdh, pdl, pdc, pdo = self.extract_previous_day_levels(df)
        swings = self.detect_swings(df)
        last_high = None
        last_high_ts = None
        last_low = None
        last_low_ts = None

        for s in reversed(swings):
            if s.swing_type == "HIGH" and last_high is None:
                last_high = s.price
                last_high_ts = s.timestamp
            elif s.swing_type == "LOW" and last_low is None:
                last_low = s.price
                last_low_ts = s.timestamp
            if last_high is not None and last_low is not None:
                break

        ltp = current_ltp
        if ltp is None and not df.empty:
            ltp = float(df["close"].iloc[-1])

        dist_pdh = None
        dist_pdl = None
        if ltp is not None and ltp > 0:
            if pdh is not None:
                dist_pdh = round(((ltp - pdh) / pdh) * 100.0, 2)
            if pdl is not None:
                dist_pdl = round(((ltp - pdl) / pdl) * 100.0, 2)

        return MarketStructureFacts(
            pdh=pdh,
            pdl=pdl,
            pdc=pdc,
            pdo=pdo,
            last_swing_high=last_high,
            last_swing_low=last_low,
            swing_high_timestamp=last_high_ts,
            swing_low_timestamp=last_low_ts,
            distance_to_pdh_pct=dist_pdh,
            distance_to_pdl_pct=dist_pdl,
        )
