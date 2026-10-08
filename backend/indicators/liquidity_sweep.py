from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
import pandas as pd
from backend.indicators.swing_structure import SwingStructureEngine


@dataclass
class LiquiditySweepFacts:
    sweep_type: str = "NONE"
    swept_level: Optional[float] = None
    swept_level_type: Optional[str] = None
    penetration_size: Optional[float] = None
    reclaim_distance: Optional[float] = None
    sweep_timestamp: Optional[datetime] = None


class LiquiditySweepEngine:
    def __init__(self, tolerance_pct: float = 0.5):
        self.tolerance_pct = tolerance_pct
        self.swing_engine = SwingStructureEngine()

    def detect_sweep(self, df: pd.DataFrame) -> LiquiditySweepFacts:
        if df.empty or len(df) < 3:
            return LiquiditySweepFacts()

        df_calc = df.copy()
        df_calc["datetime"] = pd.to_datetime(df_calc["datetime"])
        df_calc = df_calc.sort_values("datetime").reset_index(drop=True)

        history_df = df_calc.iloc[:-1]
        latest_bar = df_calc.iloc[-1]

        facts = self.swing_engine.calculate_facts(history_df)
        levels_to_check: List[Dict[str, Any]] = []

        if facts.pdh is not None:
            levels_to_check.append({"name": "PDH", "price": facts.pdh, "side": "HIGH"})
        if facts.pdl is not None:
            levels_to_check.append({"name": "PDL", "price": facts.pdl, "side": "LOW"})
        if facts.last_swing_high is not None:
            levels_to_check.append({"name": "SWING_HIGH", "price": facts.last_swing_high, "side": "HIGH"})
        if facts.last_swing_low is not None:
            levels_to_check.append({"name": "SWING_LOW", "price": facts.last_swing_low, "side": "LOW"})

        bar_open = float(latest_bar["open"])
        bar_high = float(latest_bar["high"])
        bar_low = float(latest_bar["low"])
        bar_close = float(latest_bar["close"])
        bar_ts = pd.Timestamp(latest_bar["datetime"]).to_pydatetime()

        for lvl in levels_to_check:
            lvl_price = lvl["price"]
            side = lvl["side"]
            lvl_name = lvl["name"]

            if side == "LOW":
                if bar_low < lvl_price and bar_close > lvl_price:
                    penetration = round(lvl_price - bar_low, 2)
                    reclaim = round(bar_close - lvl_price, 2)
                    return LiquiditySweepFacts(
                        sweep_type="BULLISH_SWEEP",
                        swept_level=lvl_price,
                        swept_level_type=lvl_name,
                        penetration_size=penetration,
                        reclaim_distance=reclaim,
                        sweep_timestamp=bar_ts,
                    )
            elif side == "HIGH":
                if bar_high > lvl_price and bar_close < lvl_price:
                    penetration = round(bar_high - lvl_price, 2)
                    reclaim = round(lvl_price - bar_close, 2)
                    return LiquiditySweepFacts(
                        sweep_type="BEARISH_SWEEP",
                        swept_level=lvl_price,
                        swept_level_type=lvl_name,
                        penetration_size=penetration,
                        reclaim_distance=reclaim,
                        sweep_timestamp=bar_ts,
                    )

        return LiquiditySweepFacts()
