from dataclasses import dataclass
from datetime import datetime, time
from typing import Dict, List, Optional, Tuple
import pandas as pd

Level = Tuple[float, int, int]


@dataclass
class BookSnapshot:
    timestamp: datetime
    bids: List[Level]
    asks: List[Level]
    ltp: float

    fut_ltp: Optional[float] = None
    fut_oi: Optional[float] = None

    sector_ret_30m: Optional[float] = None
    stock_ret_30m: Optional[float] = None

    circuit_lower: Optional[float] = None
    circuit_upper: Optional[float] = None

    futures_updated_at: Optional[datetime] = None
    sector_return_updated_at: Optional[datetime] = None
    stock_return_updated_at: Optional[datetime] = None


class PeerContext:
    def __init__(self, leader: pd.DataFrame, market: pd.DataFrame, sector: pd.DataFrame):
        self.frames: Dict[str, pd.DataFrame] = {}
        for name, df in (("leader", leader), ("market", market), ("sector", sector)):
            d = df.copy()
            d["datetime"] = pd.to_datetime(d["datetime"])
            self.frames[name] = d.sort_values("datetime").set_index("datetime")

    def close_back(self, name: str, ts: datetime, back: int) -> Optional[float]:
        idx = self.frames[name].index
        pos = idx.searchsorted(pd.Timestamp(ts), side="right") - 1
        if pos - back < 0 or pos < 0:
            return None
        return float(self.frames[name]["close"].iloc[pos - back])

    def before(self, ts) -> pd.DataFrame:
        cut = pd.Timestamp(ts)
        out = pd.concat(
            {k: v["close"] for k, v in self.frames.items()}, axis=1, sort=False
        ).dropna()
        out["leader_vol"] = self.frames["leader"]["volume"].reindex(out.index) if "volume" in self.frames["leader"] else 0.0
        return out[out.index < cut]
