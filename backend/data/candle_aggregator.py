from __future__ import annotations
from collections import deque
from datetime import date, datetime, time, timedelta
import logging
import math
import threading
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
import pandas as pd
from backend.data.time_utils import now_ist_naive
logger = logging.getLogger(__name__)
NSE_SESSION_OPEN = time(9, 15)
NSE_SESSION_CLOSE = time(15, 30)

def _normalize_ist_naive(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    try:
        if ts.tzinfo is not None:
            ts = ts.tz_convert('Asia/Kolkata').tz_localize(None)
    except (TypeError, ValueError):
        return None
    return ts.to_pydatetime()

def _is_finite_positive(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0.0

def _safe_non_negative_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            f_val = float(value)
        elif isinstance(value, str):
            f_val = float(value.strip())
        else:
            return None
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f_val) or f_val < 0:
        return None
    if not f_val.is_integer():
        return None
    return int(f_val)

class Candle:

    def __init__(self, symbol: str, start_time: datetime, timeframe_minutes: int):
        if timeframe_minutes <= 0:
            raise ValueError('timeframe_minutes must be > 0')
        self.symbol = str(symbol).strip().upper()
        self.start_time = start_time
        self.end_time = start_time + timedelta(minutes=int(timeframe_minutes))
        self.timeframe_minutes = int(timeframe_minutes)
        self.open: Optional[float] = None
        self.high: Optional[float] = None
        self.low: Optional[float] = None
        self.close: Optional[float] = None
        self.volume: int = 0
        self.is_completed: bool = False

    def update(self, price: float, volume: int=0) -> None:
        price = float(price)
        volume = int(volume)
        if not math.isfinite(price) or price <= 0:
            raise ValueError('Candle price must be finite and > 0')
        if volume < 0:
            raise ValueError('Candle volume must be >= 0')
        if self.open is None:
            self.open = price
            self.high = price
            self.low = price
            self.close = price
        else:
            self.high = max(float(self.high), price)
            self.low = min(float(self.low), price)
            self.close = price
        self.volume += volume

    def finalize(self) -> None:
        if self.open is None:
            raise ValueError('Cannot finalize an empty candle')
        self.is_completed = True

    def to_dict(self) -> dict:
        return {'symbol': self.symbol, 'datetime': self.start_time, 'end_time': self.end_time, 'open': self.open, 'high': self.high, 'low': self.low, 'close': self.close, 'volume': self.volume}

class CandleAggregator:

    def __init__(self, symbol: str, timeframe_minutes: int=15, on_candle_close: Optional[Callable[[dict, float], None]]=None, session_open: time=NSE_SESSION_OPEN, session_close: time=NSE_SESSION_CLOSE, max_completed_candles: int=512, require_vwap_for_callback: bool=True):
        if timeframe_minutes <= 0:
            raise ValueError('timeframe_minutes must be > 0')
        if session_open >= session_close:
            raise ValueError('session_open must be earlier than session_close')
        if max_completed_candles <= 0:
            raise ValueError('max_completed_candles must be > 0')
        self.symbol = str(symbol).strip().upper()
        self.timeframe_minutes = int(timeframe_minutes)
        self.on_candle_close = on_candle_close
        self.require_vwap_for_callback = bool(require_vwap_for_callback)
        self.session_open = session_open
        self.session_close = session_close
        self.current_candle: Optional[Candle] = None
        self.completed_candles: Deque[dict] = deque(maxlen=int(max_completed_candles))
        self.cum_pv: float = 0.0
        self.cum_vol: int = 0
        self.current_vwap: float = 0.0
        self.vwap_source: str = 'UNAVAILABLE'
        self.session_date: Optional[date] = None
        self.last_tick_timestamp: Optional[datetime] = None
        self._continuous_run_length: int = 0
        self.detected_gaps: Deque[dict] = deque(maxlen=64)
        self._lock = threading.RLock()

    def _gap_info_locked(self, start_time: datetime) -> Tuple[int, Optional[datetime], int]:
        step_seconds = self.timeframe_minutes * 60
        if self.completed_candles:
            previous_start = self.completed_candles[-1]['datetime']
            expected_start = previous_start + timedelta(minutes=self.timeframe_minutes)
            missing = max(0, int((start_time - expected_start).total_seconds() // step_seconds))
            return (missing, previous_start, 0)
        session_start = datetime.combine(start_time.date(), self.session_open)
        leading = max(0, int((start_time - session_start).total_seconds() // step_seconds))
        return (0, None, leading)

    def _reset_daily_session_locked(self, session_date: date) -> None:
        self.session_date = session_date
        self.current_candle = None
        self.completed_candles.clear()
        self.cum_pv = 0.0
        self.cum_vol = 0
        self.current_vwap = 0.0
        self.vwap_source = 'UNAVAILABLE'
        self.last_tick_timestamp = None
        self._continuous_run_length = 0
        self.detected_gaps.clear()

    def reset_daily_session(self) -> None:
        now_ist = now_ist_naive()
        with self._lock:
            self._reset_daily_session_locked(now_ist.date())

    def _ensure_session_locked(self, timestamp: datetime) -> bool:
        if timestamp.time() < self.session_open:
            return False
        if timestamp.time() > self.session_close:
            return False
        if self.session_date != timestamp.date():
            self._reset_daily_session_locked(timestamp.date())
        return True

    def _candle_start(self, timestamp: datetime) -> datetime:
        session_start = datetime.combine(timestamp.date(), self.session_open)
        elapsed_seconds = (timestamp - session_start).total_seconds()
        bucket_seconds = self.timeframe_minutes * 60
        bucket_index = int(elapsed_seconds // bucket_seconds)
        return session_start + timedelta(seconds=bucket_index * bucket_seconds)

    def _set_exchange_vwap(self, average_traded_price: Any, cumulative_volume: Any) -> bool:
        if not _is_finite_positive(average_traded_price):
            return False
        cumulative = _safe_non_negative_int(cumulative_volume)
        if cumulative is None or cumulative <= 0:
            return False
        atp = float(average_traded_price)
        cumulative_pv = atp * cumulative
        if not math.isfinite(cumulative_pv) or cumulative_pv <= 0:
            return False
        self.cum_vol = cumulative
        self.cum_pv = cumulative_pv
        self.current_vwap = atp
        self.vwap_source = 'KITE_ATP'
        return True

    def _update_observed_vwap(self, price: float, volume: int) -> None:
        if volume <= 0:
            return
        if not math.isfinite(price) or price <= 0:
            return
        self.cum_pv += price * volume
        self.cum_vol += volume
        if self.cum_vol <= 0:
            return
        self.current_vwap = self.cum_pv / self.cum_vol
        self.vwap_source = 'OBSERVED_PARTIAL'

    def _finalize_current_candle_locked(self) -> Optional[Tuple[dict, float]]:
        candle = self.current_candle
        if candle is None or candle.open is None:
            return None
        candle.finalize()
        completed_vwap = float(self.current_vwap) if self.current_vwap is not None and math.isfinite(self.current_vwap) and (self.current_vwap > 0) else 0.0
        payload = candle.to_dict()
        payload['vwap'] = completed_vwap if completed_vwap > 0 and math.isfinite(completed_vwap) else None
        payload['vwap_source'] = self.vwap_source
        missing, previous_start, leading = self._gap_info_locked(candle.start_time)
        if missing > 0:
            self._continuous_run_length = 1
            self.detected_gaps.append({'symbol': self.symbol, 'gap_start': previous_start + timedelta(minutes=self.timeframe_minutes), 'gap_end': candle.start_time, 'missing_intervals': missing})
            logger.warning('[%s] Candle gap detected before %s: %d missing interval(s) after %s. No synthetic candles created.', self.symbol, candle.start_time, missing, previous_start)
        else:
            self._continuous_run_length += 1
        payload['gap_before'] = missing > 0
        payload['missing_intervals_before'] = missing
        payload['previous_candle_start'] = previous_start
        payload['leading_missing_intervals'] = leading
        payload['continuous_run_length'] = self._continuous_run_length
        self.completed_candles.append(payload)
        self.current_candle = None
        return (payload, completed_vwap)

    def _emit_callback(self, payload: Optional[Tuple[dict, float]]) -> None:
        if payload is None:
            return
        candle_dict, completed_vwap = payload
        if completed_vwap <= 0 or not math.isfinite(completed_vwap):
            if self.require_vwap_for_callback:
                logger.warning('[%s] Completed candle %s has no valid VWAP; candle retained but strategy callback skipped.', self.symbol, candle_dict.get('datetime'))
                return
            completed_vwap = 0.0
        callback = self.on_candle_close
        if callback is None:
            return
        try:
            callback(candle_dict, completed_vwap)
        except Exception:
            logger.exception('[%s] Candle-close callback failed for %s', self.symbol, candle_dict.get('datetime'))

    def flush_if_due(self, now: Optional[datetime]=None) -> None:
        normalized_now = _normalize_ist_naive(now or now_ist_naive())
        if normalized_now is None:
            return
        callback_payload = None
        with self._lock:
            candle = self.current_candle
            if candle is None:
                return
            if normalized_now < candle.end_time:
                return
            callback_payload = self._finalize_current_candle_locked()
        self._emit_callback(callback_payload)

    def process_tick(self, price: float, volume: int, timestamp: datetime, average_traded_price: Optional[float]=None, cumulative_volume: Optional[int]=None) -> None:
        normalized_ts = _normalize_ist_naive(timestamp)
        if normalized_ts is None:
            logger.warning('[%s] Dropping tick with invalid timestamp: %r', self.symbol, timestamp)
            return
        try:
            px = float(price)
        except (TypeError, ValueError):
            logger.warning('[%s] Dropping tick with invalid price: %r', self.symbol, price)
            return
        if not math.isfinite(px) or px <= 0:
            logger.warning('[%s] Dropping tick with invalid price: %r', self.symbol, price)
            return
        vol = _safe_non_negative_int(volume)
        if vol is None:
            logger.warning('[%s] Dropping tick with invalid volume: %r', self.symbol, volume)
            return
        callback_payload = None
        with self._lock:
            if not self._ensure_session_locked(normalized_ts):
                return
            if self.last_tick_timestamp is not None and normalized_ts < self.last_tick_timestamp:
                logger.warning('[%s] Dropping out-of-order tick %s < %s', self.symbol, normalized_ts, self.last_tick_timestamp)
                return
            self.last_tick_timestamp = normalized_ts
            candle_start = self._candle_start(normalized_ts)
            if normalized_ts.time() >= self.session_close:
                if self.current_candle is not None:
                    callback_payload = self._finalize_current_candle_locked()
            else:
                if self.current_candle is not None and candle_start >= self.current_candle.end_time:
                    callback_payload = self._finalize_current_candle_locked()
                if self.current_candle is None:
                    self.current_candle = Candle(symbol=self.symbol, start_time=candle_start, timeframe_minutes=self.timeframe_minutes)
                self.current_candle.update(price=px, volume=vol)
                exchange_vwap_ok = False
                if average_traded_price is not None and cumulative_volume is not None:
                    exchange_vwap_ok = self._set_exchange_vwap(average_traded_price, cumulative_volume)
                if not exchange_vwap_ok:
                    self._update_observed_vwap(price=px, volume=vol)
        self._emit_callback(callback_payload)

    @staticmethod
    def _rows_to_dataframe(rows: List[dict]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        for column in ('datetime', 'end_time', 'previous_candle_start'):
            if column in df.columns:
                df[column] = pd.to_datetime(df[column])
        return df

    def get_completed_dataframe(self) -> pd.DataFrame:
        with self._lock:
            rows = list(self.completed_candles)
        return self._rows_to_dataframe(rows)

    def get_continuous_dataframe(self, min_candles: int=1) -> pd.DataFrame:
        required = max(1, int(min_candles))
        with self._lock:
            run = self._continuous_run_length
            if run < required or not self.completed_candles:
                return pd.DataFrame()
            rows = list(self.completed_candles)[-run:]
        return self._rows_to_dataframe(rows)

    def is_continuous(self, min_candles: int=1) -> bool:
        required = max(1, int(min_candles))
        with self._lock:
            return bool(self.completed_candles) and self._continuous_run_length >= required

    def get_continuity_status(self) -> dict:
        with self._lock:
            candle = self.current_candle
            current_missing = 0
            if candle is not None:
                current_missing, _, _ = self._gap_info_locked(candle.start_time)
            return {'symbol': self.symbol, 'timeframe_minutes': self.timeframe_minutes, 'completed_candles': len(self.completed_candles), 'continuous_run_length': self._continuous_run_length, 'has_gap': len(self.detected_gaps) > 0, 'gap_count': len(self.detected_gaps), 'gaps': [dict(gap) for gap in self.detected_gaps], 'current_candle_missing_intervals': current_missing}

    def get_current_vwap(self) -> Optional[float]:
        with self._lock:
            if self.current_vwap is not None and math.isfinite(self.current_vwap) and (self.current_vwap > 0):
                return float(self.current_vwap)
            return None

    def get_current_candle(self) -> Optional[dict]:
        with self._lock:
            candle = self.current_candle
            if candle is None:
                return None
            payload = candle.to_dict()
            missing, previous_start, leading = self._gap_info_locked(candle.start_time)
            payload['gap_before'] = missing > 0
            payload['missing_intervals_before'] = missing
            payload['previous_candle_start'] = previous_start
            payload['leading_missing_intervals'] = leading
            return payload

    def invalidate_gap_state(self) -> None:
        with self._lock:
            self.current_candle = None
            self.cum_pv = 0.0
            self.cum_vol = 0
            self.current_vwap = 0.0
            self.vwap_source = 'UNAVAILABLE'

    def discard_in_progress_candle(self) -> None:
        with self._lock:
            self.current_candle = None

class MultiSymbolCandleAggregator:

    def __init__(self, token_to_symbol_map: Dict[int, str], timeframe_minutes: int=15, on_candle_close: Optional[Callable[[dict, float], None]]=None, on_book_update: Optional[Callable[[str, Any], None]]=None, max_completed_candles_per_symbol: int=512, require_vwap_for_callback: bool=True):
        if not token_to_symbol_map:
            raise ValueError('token_to_symbol_map must contain at least one instrument')
        if timeframe_minutes <= 0:
            raise ValueError('timeframe_minutes must be > 0')
        if max_completed_candles_per_symbol <= 0:
            raise ValueError('max_completed_candles_per_symbol must be > 0')
        self.token_to_symbol_map: Dict[int, str] = {}
        self.symbol_to_token: Dict[str, int] = {}
        for raw_token, raw_symbol in token_to_symbol_map.items():
            try:
                token = int(raw_token)
            except (TypeError, ValueError) as exc:
                raise ValueError(f'Invalid instrument token: {raw_token!r}') from exc
            if token <= 0:
                raise ValueError(f'Instrument token must be > 0: {token}')
            symbol = str(raw_symbol).strip().upper()
            if not symbol:
                raise ValueError(f'Instrument symbol is empty for token {token}')
            previous_symbol = self.token_to_symbol_map.get(token)
            if previous_symbol is not None and previous_symbol != symbol:
                raise ValueError(f'Token {token} maps to multiple symbols: {previous_symbol!r} and {symbol!r}')
            previous_token = self.symbol_to_token.get(symbol)
            if previous_token is not None and previous_token != token:
                raise ValueError(f'Symbol {symbol!r} maps to multiple tokens: {previous_token} and {token}')
            self.token_to_symbol_map[token] = symbol
            self.symbol_to_token[symbol] = token
        self.timeframe_minutes = int(timeframe_minutes)
        self.on_candle_close = on_candle_close
        self.on_book_update = on_book_update
        self.max_completed_candles_per_symbol = int(max_completed_candles_per_symbol)
        self.require_vwap_for_callback = bool(require_vwap_for_callback)
        self.aggregators: Dict[str, CandleAggregator] = {}
        self.latest_book_snapshots: Dict[str, Any] = {}
        self.last_volume_by_token: Dict[int, int] = {}
        self.volume_session_date_by_token: Dict[int, date] = {}
        self.last_tick_timestamp_by_token: Dict[int, datetime] = {}
        self._lock = threading.RLock()
        self._init_aggregators()

    def _init_aggregators(self) -> None:
        with self._lock:
            for symbol in self.symbol_to_token:
                self.aggregators[symbol] = CandleAggregator(symbol=symbol, timeframe_minutes=self.timeframe_minutes, on_candle_close=self.on_candle_close, max_completed_candles=self.max_completed_candles_per_symbol, require_vwap_for_callback=self.require_vwap_for_callback)

    def _calculate_incremental_volume(self, token: int, timestamp: datetime, cumulative_volume: Optional[int], last_traded_quantity: Optional[int]) -> int:
        if cumulative_volume is None:
            return last_traded_quantity or 0
        session_date = timestamp.date()
        previous_session = self.volume_session_date_by_token.get(token)
        if previous_session != session_date:
            self.volume_session_date_by_token[token] = session_date
            self.last_volume_by_token[token] = cumulative_volume
            if last_traded_quantity is not None:
                return min(last_traded_quantity, cumulative_volume)
            return 0
        previous_volume = self.last_volume_by_token.get(token)
        if previous_volume is None:
            self.last_volume_by_token[token] = cumulative_volume
            if last_traded_quantity is not None:
                return min(last_traded_quantity, cumulative_volume)
            return 0
        if cumulative_volume < previous_volume:
            logger.warning('Cumulative volume decreased for token %s: %s -> %s. Re-baselining.', token, previous_volume, cumulative_volume)
            self.last_volume_by_token[token] = cumulative_volume
            if last_traded_quantity is not None:
                return min(last_traded_quantity, cumulative_volume)
            return 0
        delta = cumulative_volume - previous_volume
        self.last_volume_by_token[token] = cumulative_volume
        return max(delta, 0)

    @staticmethod
    def _parse_depth_snapshot(tick: Dict[str, Any], timestamp: datetime, price: float) -> Optional[Any]:
        depth = tick.get('depth')
        if not isinstance(depth, dict):
            return None
        buy_levels = depth.get('buy')
        sell_levels = depth.get('sell')
        if not isinstance(buy_levels, list) or not isinstance(sell_levels, list):
            return None
        if len(buy_levels) < 5 or len(sell_levels) < 5:
            return None
        bids: List[Tuple[float, int, int]] = []
        asks: List[Tuple[float, int, int]] = []
        try:
            for level in buy_levels[:5]:
                if not isinstance(level, dict):
                    return None
                bid_price = float(level.get('price', 0))
                bid_qty = int(level.get('quantity', 0))
                bid_orders = int(level.get('orders', 0))
                if bid_price == 0:
                    return None
                if not math.isfinite(bid_price) or bid_price < 0 or bid_qty < 0 or (bid_orders < 0):
                    return None
                bids.append((bid_price, bid_qty, bid_orders))
            for level in sell_levels[:5]:
                if not isinstance(level, dict):
                    return None
                ask_price = float(level.get('price', 0))
                ask_qty = int(level.get('quantity', 0))
                ask_orders = int(level.get('orders', 0))
                if ask_price == 0:
                    return None
                if not math.isfinite(ask_price) or ask_price < 0 or ask_qty < 0 or (ask_orders < 0):
                    return None
                asks.append((ask_price, ask_qty, ask_orders))
            if len(bids) != 5 or len(asks) != 5:
                return None
            for i in range(1, len(bids)):
                if bids[i][0] > bids[i - 1][0]:
                    return None
            for i in range(1, len(asks)):
                if asks[i][0] < asks[i - 1][0]:
                    return None
            if bids[0][0] >= asks[0][0]:
                return None
            from backend.data.models import BookSnapshot
            oi_raw = tick.get('oi')
            lower_raw = tick.get('lower_circuit_limit')
            upper_raw = tick.get('upper_circuit_limit')
            oi = float(oi_raw) if _is_finite_positive(oi_raw) else None
            circuit_lower = float(lower_raw) if _is_finite_positive(lower_raw) else None
            circuit_upper = float(upper_raw) if _is_finite_positive(upper_raw) else None
            return BookSnapshot(timestamp=timestamp, bids=bids, asks=asks, ltp=price, fut_ltp=None, fut_oi=None, circuit_lower=circuit_lower, circuit_upper=circuit_upper)
        except (TypeError, ValueError, KeyError, ImportError):
            logger.debug('Ignoring malformed/unavailable Level-5 depth for token %s', tick.get('instrument_token'), exc_info=True)
            return None

    def process_ticks(self, ticks: Any) -> Dict[str, int]:
        if isinstance(ticks, dict):
            tick_batch = [ticks]
        elif isinstance(ticks, list):
            tick_batch = ticks
        else:
            return {}
        applied_volume_by_symbol: Dict[str, int] = {}
        for tick in tick_batch:
            if not isinstance(tick, dict):
                continue
            raw_token = tick.get('instrument_token')
            try:
                token = int(raw_token)
            except (TypeError, ValueError):
                logger.warning('Dropping tick with invalid instrument_token: %r', raw_token)
                continue
            with self._lock:
                symbol = self.token_to_symbol_map.get(token)
            if symbol is None:
                logger.warning('Ignoring tick for unsubscribed token %s', token)
                continue
            raw_price = tick.get('last_price')
            if not _is_finite_positive(raw_price):
                logger.warning('[%s] Dropping tick with invalid last_price=%r', symbol, raw_price)
                continue
            price = float(raw_price)
            raw_timestamp = tick.get('exchange_timestamp') or tick.get('timestamp')
            timestamp = _normalize_ist_naive(raw_timestamp)
            if timestamp is None:
                logger.warning('[%s] Dropping tick because exchange timestamp is missing or invalid.', symbol)
                continue
            cumulative_volume = _safe_non_negative_int(tick.get('volume_traded'))
            raw_last_qty = tick.get('last_traded_quantity')
            if raw_last_qty is None:
                raw_last_qty = tick.get('last_quantity')
            last_traded_quantity = _safe_non_negative_int(raw_last_qty)
            with self._lock:
                previous_timestamp = self.last_tick_timestamp_by_token.get(token)
                if previous_timestamp is not None and timestamp < previous_timestamp:
                    logger.warning('[%s] Dropping out-of-order tick %s < %s', symbol, timestamp, previous_timestamp)
                    continue
                self.last_tick_timestamp_by_token[token] = timestamp
                incremental_volume = self._calculate_incremental_volume(token=token, timestamp=timestamp, cumulative_volume=cumulative_volume, last_traded_quantity=last_traded_quantity)
                aggregator = self.aggregators.get(symbol)
            if aggregator is None:
                continue
            if incremental_volume > 0:
                applied_volume_by_symbol[symbol] = applied_volume_by_symbol.get(symbol, 0) + incremental_volume
            average_traded_price = tick.get('average_traded_price')
            aggregator.process_tick(price=price, volume=incremental_volume, timestamp=timestamp, average_traded_price=average_traded_price, cumulative_volume=cumulative_volume)
            snapshot = self._parse_depth_snapshot(tick=tick, timestamp=timestamp, price=price)
            if snapshot is None:
                continue
            callback = None
            with self._lock:
                self.latest_book_snapshots[symbol] = snapshot
                callback = self.on_book_update
            if callback is not None:
                try:
                    callback(symbol, snapshot)
                except Exception:
                    logger.exception('[%s] on_book_update callback failed', symbol)
        return applied_volume_by_symbol

    def get_symbol_dataframe(self, symbol: str) -> pd.DataFrame:
        symbol_key = str(symbol).strip().upper()
        with self._lock:
            aggregator = self.aggregators.get(symbol_key)
        if aggregator is None:
            return pd.DataFrame()
        return aggregator.get_completed_dataframe()

    def get_continuous_symbol_dataframe(self, symbol: str, min_candles: int=1) -> pd.DataFrame:
        symbol_key = str(symbol).strip().upper()
        with self._lock:
            aggregator = self.aggregators.get(symbol_key)
        if aggregator is None:
            return pd.DataFrame()
        return aggregator.get_continuous_dataframe(min_candles)

    def is_symbol_continuous(self, symbol: str, min_candles: int=1) -> bool:
        symbol_key = str(symbol).strip().upper()
        with self._lock:
            aggregator = self.aggregators.get(symbol_key)
        if aggregator is None:
            return False
        return aggregator.is_continuous(min_candles)

    def get_symbol_continuity(self, symbol: str) -> dict:
        symbol_key = str(symbol).strip().upper()
        with self._lock:
            aggregator = self.aggregators.get(symbol_key)
        if aggregator is None:
            return {}
        return aggregator.get_continuity_status()

    def get_latest_book_snapshot(self, symbol: str) -> Optional[Any]:
        symbol_key = str(symbol).strip().upper()
        with self._lock:
            return self.latest_book_snapshots.get(symbol_key)

    def get_current_vwap(self, symbol: str) -> float:
        symbol_key = str(symbol).strip().upper()
        with self._lock:
            aggregator = self.aggregators.get(symbol_key)
        if aggregator is None:
            return 0.0
        return aggregator.get_current_vwap()

    def get_current_candle(self, symbol: str) -> Optional[dict]:
        symbol_key = str(symbol).strip().upper()
        with self._lock:
            aggregator = self.aggregators.get(symbol_key)
        if aggregator is None:
            return None
        return aggregator.get_current_candle()

    def reset_all_daily_sessions(self) -> None:
        with self._lock:
            for aggregator in self.aggregators.values():
                aggregator.reset_daily_session()
            self.last_volume_by_token.clear()
            self.volume_session_date_by_token.clear()
            self.last_tick_timestamp_by_token.clear()
            self.latest_book_snapshots.clear()

    def handle_connection_gap(self) -> None:
        with self._lock:
            aggregators = list(self.aggregators.values())
            self.last_volume_by_token.clear()
            self.volume_session_date_by_token.clear()
            self.latest_book_snapshots.clear()
        for aggregator in aggregators:
            aggregator.discard_in_progress_candle()
            aggregator.invalidate_gap_state()

    def flush_due_candles(self, now: Optional[datetime]=None) -> None:
        with self._lock:
            aggregators = list(self.aggregators.values())
        for agg in aggregators:
            try:
                agg.flush_if_due(now=now)
            except Exception:
                logger.exception('[MultiSymbolCandleAggregator] Error flushing due candle for %s', agg.symbol)