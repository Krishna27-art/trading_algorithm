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
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"])
        return df


class MultiSymbolCandleAggregator:
    """
    Manages CandleAggregator instances for multiple symbols/tokens simultaneously.
    Consumes live Zerodha KiteTicker ticks (from on_ticks callback) and routes them to
    their respective per-symbol CandleAggregators. Emits completed candles for strategy processing.
    """

    def __init__(
        self,
        token_to_symbol_map: Dict[int, str],
        timeframe_minutes: int = 15,
        on_candle_close: Optional[Callable[[dict, float], None]] = None,
    ):
        self.token_to_symbol_map = {int(k): str(v) for k, v in token_to_symbol_map.items()}
        self.timeframe_minutes = timeframe_minutes
        self.on_candle_close = on_candle_close
        self.aggregators: Dict[str, CandleAggregator] = {}
        self.last_volume_by_token: Dict[int, int] = {}
        self._init_aggregators()

    def _init_aggregators(self):
        for token, symbol in self.token_to_symbol_map.items():
            if symbol not in self.aggregators:
                self.aggregators[symbol] = CandleAggregator(
                    symbol=symbol,
                    timeframe_minutes=self.timeframe_minutes,
                    on_candle_close=self.on_candle_close,
                )

    def process_ticks(self, ticks: Any):
        """
        Processes a batch of raw Zerodha KiteTicker tick objects.
        """
        if not isinstance(ticks, list):
            if isinstance(ticks, dict):
                ticks = [ticks]
            else:
                return

        for tick in ticks:
            if not isinstance(tick, dict):
                continue

            token = tick.get("instrument_token")
            if token is None:
                continue

            symbol = self.token_to_symbol_map.get(int(token))
            if not symbol:
                continue

            price = tick.get("last_price")
            if price is None or price <= 0:
                continue

            last_qty = tick.get("last_traded_quantity") or tick.get("last_quantity")
            vol_traded = tick.get("volume_traded") or tick.get("volume")

            if last_qty is not None and last_qty > 0:
                volume = int(last_qty)
            elif vol_traded is not None:
                prev_vol = self.last_volume_by_token.get(int(token), 0)
                volume = max(int(vol_traded) - prev_vol, 0) if prev_vol > 0 else 0
                self.last_volume_by_token[int(token)] = int(vol_traded)
            else:
                volume = 0

            raw_ts = tick.get("exchange_timestamp") or tick.get("timestamp")
            if isinstance(raw_ts, datetime):
                timestamp = raw_ts
            elif isinstance(raw_ts, str):
                try:
                    timestamp = datetime.fromisoformat(raw_ts)
                except Exception:
                    timestamp = datetime.now()
            else:
                timestamp = datetime.now()

            agg = self.aggregators.get(symbol)
            if agg:
                agg.process_tick(price=float(price), volume=volume, timestamp=timestamp)

    def get_symbol_dataframe(self, symbol: str) -> pd.DataFrame:
        agg = self.aggregators.get(symbol)
        return agg.get_completed_dataframe() if agg else pd.DataFrame()

    def reset_all_daily_sessions(self):
        for agg in self.aggregators.values():
            agg.reset_daily_session()
        self.last_volume_by_token.clear()
