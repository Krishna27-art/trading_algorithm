"""
Real-time tick-to-candle aggregator with VWAP calculation for WebSocket data feeds.
"""

from datetime import datetime, time, timedelta
from typing import Callable, Dict, List, Optional
import pandas as pd


class Candle:
    def __init__(self, symbol: str, start_time: datetime, timeframe_minutes: int):
        self.symbol = symbol
        self.start_time = start_time
        self.end_time = start_time + timedelta(minutes=timeframe_minutes)
        self.timeframe_minutes = timeframe_minutes
        self.open: Optional[float] = None
        self.high: Optional[float] = None
        self.low: Optional[float] = None
        self.close: Optional[float] = None
        self.volume: int = 0
        self.is_completed: bool = False

    def update(self, price: float, volume: int = 1):
        if self.open is None:
            self.open = price
            self.high = price
            self.low = price
            self.close = price
        else:
            self.high = max(self.high, price)
            self.low = min(self.low, price)
            self.close = price
        self.volume += volume

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "datetime": self.start_time,
            "end_time": self.end_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


class CandleAggregator:
    def __init__(
        self,
        symbol: str,
        timeframe_minutes: int = 15,
        on_candle_close: Optional[Callable[[dict, float], None]] = None,
    ):
        self.symbol = symbol
        self.timeframe_minutes = timeframe_minutes
        self.on_candle_close = on_candle_close
        self.current_candle: Optional[Candle] = None
        self.completed_candles: List[dict] = []

        # Session VWAP state
        self.cum_pv: float = 0.0
        self.cum_vol: int = 0
        self.current_vwap: float = 0.0

    def reset_daily_session(self):
        """Resets candle and VWAP states at the start of a trading day."""
        self.current_candle = None
        self.completed_candles = []
        self.cum_pv = 0.0
        self.cum_vol = 0
        self.current_vwap = 0.0

    def process_tick(self, price: float, volume: int, timestamp: datetime):
        # Calculate candle boundary
        minute = timestamp.minute - (timestamp.minute % self.timeframe_minutes)
        candle_start = timestamp.replace(minute=minute, second=0, microsecond=0)

        # Update continuous session VWAP
        if volume > 0:
            self.cum_pv += price * volume
            self.cum_vol += volume
            self.current_vwap = self.cum_pv / self.cum_vol

        # Check if new candle has started
        if self.current_candle is None:
            self.current_candle = Candle(self.symbol, candle_start, self.timeframe_minutes)
            self.current_candle.update(price, volume)
        elif timestamp >= self.current_candle.end_time:
            # Complete the previous candle
            self.current_candle.is_completed = True
            c_dict = self.current_candle.to_dict()
            self.completed_candles.append(c_dict)

            # Fire callback for completed candle
            if self.on_candle_close:
                self.on_candle_close(c_dict, self.current_vwap)

            # Start new candle
            self.current_candle = Candle(self.symbol, candle_start, self.timeframe_minutes)
            self.current_candle.update(price, volume)
        else:
            # Continuing current candle
            self.current_candle.update(price, volume)

    def get_completed_dataframe(self) -> pd.DataFrame:
        if not self.completed_candles:
            return pd.DataFrame()
        df = pd.DataFrame(self.completed_candles)
        df.set_index("datetime", inplace=True)
        return df
