from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
import pandas as pd


@dataclass
class FVGRecord:
    fvg_id: str
    fvg_type: str
    upper_price: float
    lower_price: float
    created_at: datetime
    gap_size: float
    fill_percentage: float = 0.0
    status: str = "ACTIVE"


@dataclass
class FVGFacts:
    active_fvgs: List[FVGRecord]
    latest_fvg: Optional[FVGRecord] = None
    in_fvg_zone: bool = False


class FairValueGapEngine:
    def __init__(self, min_gap_pct: float = 0.1):
        self.min_gap_pct = min_gap_pct

    def detect_fvgs(self, df: pd.DataFrame, current_ltp: Optional[float] = None) -> FVGFacts:
        if df.empty or len(df) < 3:
            return FVGFacts(active_fvgs=[])

        df_calc = df.copy()
        df_calc["datetime"] = pd.to_datetime(df_calc["datetime"])
        df_calc = df_calc.sort_values("datetime").reset_index(drop=True)

        fvgs: List[FVGRecord] = []
        n = len(df_calc)

        for i in range(2, n):
            c1 = df_calc.iloc[i - 2]
            c2 = df_calc.iloc[i - 1]
            c3 = df_calc.iloc[i]

            c1_high = float(c1["high"])
            c1_low = float(c1["low"])
            c3_high = float(c3["high"])
            c3_low = float(c3["low"])
            ts = pd.Timestamp(c3["datetime"]).to_pydatetime()

            if c3_low > c1_high:
                gap = round(c3_low - c1_high, 2)
                gap_pct = (gap / c1_high) * 100.0 if c1_high > 0 else 0.0
                if gap_pct >= self.min_gap_pct:
                    fvg_id = f"BULLISH_FVG_{i}_{ts.strftime('%Y%m%d%H%M')}"
                    fvgs.append(FVGRecord(
                        fvg_id=fvg_id,
                        fvg_type="BULLISH",
                        upper_price=c3_low,
                        lower_price=c1_high,
                        created_at=ts,
                        gap_size=gap,
                    ))

            elif c3_high < c1_low:
                gap = round(c1_low - c3_high, 2)
                gap_pct = (gap / c1_low) * 100.0 if c1_low > 0 else 0.0
                if gap_pct >= self.min_gap_pct:
                    fvg_id = f"BEARISH_FVG_{i}_{ts.strftime('%Y%m%d%H%M')}"
                    fvgs.append(FVGRecord(
                        fvg_id=fvg_id,
                        fvg_type="BEARISH",
                        upper_price=c1_low,
                        lower_price=c3_high,
                        created_at=ts,
                        gap_size=gap,
                    ))

        ltp = current_ltp
        if ltp is None and not df_calc.empty:
            ltp = float(df_calc["close"].iloc[-1])

        active_list: List[FVGRecord] = []
        in_zone = False

        for fvg in fvgs:
            if ltp is not None:
                if fvg.fvg_type == "BULLISH":
                    if ltp <= fvg.lower_price:
                        fvg.status = "FILLED"
                        fvg.fill_percentage = 100.0
                    elif ltp < fvg.upper_price:
                        fvg.status = "PARTIALLY_FILLED"
                        filled_dist = fvg.upper_price - ltp
                        fvg.fill_percentage = round((filled_dist / fvg.gap_size) * 100.0, 1) if fvg.gap_size > 0 else 50.0
                        in_zone = True
                        active_list.append(fvg)
                    else:
                        active_list.append(fvg)
                elif fvg.fvg_type == "BEARISH":
                    if ltp >= fvg.upper_price:
                        fvg.status = "FILLED"
                        fvg.fill_percentage = 100.0
                    elif ltp > fvg.lower_price:
                        fvg.status = "PARTIALLY_FILLED"
                        filled_dist = ltp - fvg.lower_price
                        fvg.fill_percentage = round((filled_dist / fvg.gap_size) * 100.0, 1) if fvg.gap_size > 0 else 50.0
                        in_zone = True
                        active_list.append(fvg)
                    else:
                        active_list.append(fvg)
            else:
                active_list.append(fvg)

        latest = active_list[-1] if active_list else None

        return FVGFacts(
            active_fvgs=active_list,
            latest_fvg=latest,
            in_fvg_zone=in_zone,
        )
