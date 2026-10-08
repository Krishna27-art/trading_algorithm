from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional
import numpy as np
import pandas as pd


@dataclass
class AnchoredVWAPFacts:
    anchored_vwap: Optional[float] = None
    anchor_timestamp: Optional[datetime] = None
    anchor_price: Optional[float] = None
    price_vs_avwap: Optional[str] = None
    distance_from_avwap_pct: Optional[float] = None
    avwap_slope: Optional[float] = None


class AnchoredVWAPEngine:
    def __init__(self, default_anchor_type: str = "DAY_OPEN"):
        self.default_anchor_type = default_anchor_type

    def calculate_anchored_vwap(
        self,
        df: pd.DataFrame,
        anchor_timestamp: Optional[datetime] = None,
        anchor_type: str = "DAY_OPEN",
        current_ltp: Optional[float] = None,
    ) -> AnchoredVWAPFacts:
        if df.empty or "datetime" not in df.columns:
            return AnchoredVWAPFacts()

        df_calc = df.copy()
        df_calc["datetime"] = pd.to_datetime(df_calc["datetime"])
        df_calc = df_calc.sort_values("datetime").reset_index(drop=True)

        target_ts = anchor_timestamp
        if target_ts is None:
            if anchor_type == "DAY_OPEN":
                latest_date = df_calc["datetime"].iloc[-1].date()
                day_df = df_calc[df_calc["datetime"].dt.date == latest_date]
                if not day_df.empty:
                    target_ts = day_df["datetime"].iloc[0]
            elif anchor_type in ("SWING_HIGH", "SWING_LOW"):
                from backend.indicators.swing_structure import SwingStructureEngine
                swing_engine = SwingStructureEngine()
                swings = swing_engine.detect_swings(df_calc)
                matching = [s for s in swings if s.swing_type == ("HIGH" if anchor_type == "SWING_HIGH" else "LOW")]
                if matching:
                    target_ts = matching[-1].timestamp

        if target_ts is None:
            target_ts = df_calc["datetime"].iloc[0]

        anchor_mask = df_calc["datetime"] >= target_ts
        sub_df = df_calc[anchor_mask].copy()

        if sub_df.empty:
            return AnchoredVWAPFacts()

        anchor_price = float(sub_df["open"].iloc[0])

        if "typical_price" not in sub_df.columns:
            sub_df["typical_price"] = (sub_df["high"] + sub_df["low"] + sub_df["close"]) / 3.0

        pv = sub_df["typical_price"] * sub_df["volume"]
        cum_pv = pv.cumsum()
        cum_vol = sub_df["volume"].cumsum()

        avwap_series = np.where(cum_vol > 0, cum_pv / cum_vol, sub_df["typical_price"])

        latest_avwap = float(avwap_series[-1])

        ltp = current_ltp
        if ltp is None:
            ltp = float(sub_df["close"].iloc[-1])

        price_vs_avwap = "ABOVE" if ltp > latest_avwap else ("BELOW" if ltp < latest_avwap else "AT")
        distance_pct = round(((ltp - latest_avwap) / latest_avwap) * 100.0, 2) if latest_avwap > 0 else 0.0

        slope = 0.0
        if len(avwap_series) >= 2:
            prev_avwap = float(avwap_series[-2])
            slope = round(latest_avwap - prev_avwap, 4)

        return AnchoredVWAPFacts(
            anchored_vwap=round(latest_avwap, 2),
            anchor_timestamp=pd.Timestamp(target_ts).to_pydatetime(),
            anchor_price=round(anchor_price, 2),
            price_vs_avwap=price_vs_avwap,
            distance_from_avwap_pct=distance_pct,
            avwap_slope=slope,
        )
