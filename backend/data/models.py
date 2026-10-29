from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

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
