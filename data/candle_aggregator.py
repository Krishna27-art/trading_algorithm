"""
Real-time tick-to-candle aggregation for Zerodha KiteTicker.

Properties
----------
- Exchange timestamp is used; missing/invalid timestamps are rejected.
- Candles are anchored to the NSE session open (09:15 IST).
- The first tick of a new candle is NEVER included in the previous candle's VWAP.
- Kite average_traded_price is used as the authoritative session VWAP when available.
- Kite cumulative volume is converted to incremental candle volume.
- Duplicate/out-of-order ticks cannot silently corrupt volume/candle ordering.
- No synthetic candles are generated for missing time periods.
- Candle history is bounded in memory.
- Level-5 parsing failures are logged instead of silently swallowed.
- External callbacks are never executed while aggregation locks are held.

Complexity
----------
Per tick:
    token -> symbol lookup: O(1) average
    symbol -> aggregator lookup: O(1) average
    candle update: O(1)
    VWAP update: O(1)
    L5 extraction: O(1) because only first 5 levels are processed

Per batch of k ticks:
    O(k)

Memory:
    O(S * C + S)
    where:
        S = subscribed symbols
        C = max completed candles retained per symbol
"""

from __future__ import annotations

from collections import deque
from datetime import date, datetime, time, timedelta
import logging
import math
import threading
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import pandas as pd

from data.time_utils import now_ist_naive


logger = logging.getLogger(__name__)


NSE_SESSION_OPEN = time(9, 15)
NSE_SESSION_CLOSE = time(15, 30)


def _normalize_ist_naive(value: Any) -> Optional[datetime]:
    """
    Convert an incoming timestamp to Asia/Kolkata and return naive IST time.

    Repository contract:
    - timezone-aware input -> converted to Asia/Kolkata
    - naive input -> already treated as IST
    """
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
            ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
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
    if isinstance(value, bool):
        return None

    try:
        number = int(value)
    except (TypeError, ValueError):
        return None

    return number if number >= 0 else None


class Candle:
    """
    Single OHLCV candle.

    start_time is the candle OPEN time.
    end_time is the exclusive candle completion boundary.
    """

    def __init__(
        self,
        symbol: str,
        start_time: datetime,
        timeframe_minutes: int,
    ):
        if timeframe_minutes <= 0:
            raise ValueError("timeframe_minutes must be > 0")

        self.symbol = str(symbol).strip().upper()
        self.start_time = start_time
        self.end_time = start_time + timedelta(
            minutes=int(timeframe_minutes)
        )
        self.timeframe_minutes = int(timeframe_minutes)

        self.open: Optional[float] = None
        self.high: Optional[float] = None
        self.low: Optional[float] = None
        self.close: Optional[float] = None
        self.volume: int = 0
        self.is_completed: bool = False

    def update(
        self,
        price: float,
        volume: int = 0,
    ) -> None:
        price = float(price)
        volume = int(volume)

        if not math.isfinite(price) or price <= 0:
            raise ValueError("Candle price must be finite and > 0")

        if volume < 0:
            raise ValueError("Candle volume must be >= 0")

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
            raise ValueError("Cannot finalize an empty candle")

        self.is_completed = True

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
    """
    Aggregates ticks for one symbol into fixed-width NSE session candles.
    """

    def __init__(
        self,
        symbol: str,
        timeframe_minutes: int = 15,
        on_candle_close: Optional[
            Callable[[dict, float], None]
        ] = None,
        session_open: time = NSE_SESSION_OPEN,
        session_close: time = NSE_SESSION_CLOSE,
        max_completed_candles: int = 512,
    ):
        if timeframe_minutes <= 0:
            raise ValueError("timeframe_minutes must be > 0")

        if session_open >= session_close:
            raise ValueError(
                "session_open must be earlier than session_close"
            )

        if max_completed_candles <= 0:
            raise ValueError(
                "max_completed_candles must be > 0"
            )

        self.symbol = str(symbol).strip().upper()
        self.timeframe_minutes = int(timeframe_minutes)
        self.on_candle_close = on_candle_close

        self.session_open = session_open
        self.session_close = session_close

        self.current_candle: Optional[Candle] = None

        self.completed_candles: Deque[dict] = deque(
            maxlen=int(max_completed_candles)
        )

        # Session VWAP state.
        self.cum_pv: float = 0.0
        self.cum_vol: int = 0
        self.current_vwap: float = 0.0
        self.vwap_source: str = "UNAVAILABLE"

        # Session state.
        self.session_date: Optional[date] = None
        self.last_tick_timestamp: Optional[datetime] = None

        self._lock = threading.RLock()

    # ================================================================
    # SESSION
    # ================================================================

    def _reset_daily_session_locked(
        self,
        session_date: date,
    ) -> None:
        self.session_date = session_date

        self.current_candle = None
        self.completed_candles.clear()

        self.cum_pv = 0.0
        self.cum_vol = 0
        self.current_vwap = 0.0
        self.vwap_source = "UNAVAILABLE"

        self.last_tick_timestamp = None

    def reset_daily_session(self) -> None:
        """
        Reset this symbol's intraday state.

        The explicit system clock is Asia/Kolkata.
        """
        now_ist = now_ist_naive()

        with self._lock:
            self._reset_daily_session_locked(
                now_ist.date()
            )

    def _ensure_session_locked(
        self,
        timestamp: datetime,
    ) -> bool:
        """
        Accept only regular NSE-session timestamps.
        """
        if timestamp.time() < self.session_open:
            return False

        if timestamp.time() > self.session_close:
            return False

        if self.session_date != timestamp.date():
            self._reset_daily_session_locked(
                timestamp.date()
            )

        return True

    # ================================================================
    # CANDLE BOUNDARIES
    # ================================================================

    def _candle_start(
        self,
        timestamp: datetime,
    ) -> datetime:
        """
        Anchor candles to 09:15 IST.

        For 15-minute candles:
            09:15
            09:30
            09:45
            10:00
            ...

        Unlike a midnight modulo calculation, this also behaves correctly
        for timeframes such as 30 minutes relative to the trading session.
        """
        session_start = datetime.combine(
            timestamp.date(),
            self.session_open,
        )

        elapsed_seconds = (
            timestamp - session_start
        ).total_seconds()

        bucket_seconds = self.timeframe_minutes * 60

        bucket_index = int(
            elapsed_seconds // bucket_seconds
        )

        return session_start + timedelta(
            seconds=bucket_index * bucket_seconds
        )

    # ================================================================
    # VWAP
    # ================================================================

    def _set_exchange_vwap(
        self,
        average_traded_price: Any,
        cumulative_volume: Any,
    ) -> bool:
        """
        Use Kite's running day VWAP and cumulative traded volume.

        average_traded_price * volume_traded = cumulative traded value.

        This is preferred over reconstructing the session VWAP from sparse
        WebSocket ticks.
        """
        if not _is_finite_positive(
            average_traded_price
        ):
            return False

        cumulative = _safe_non_negative_int(
            cumulative_volume
        )

        if cumulative is None or cumulative <= 0:
            return False

        atp = float(
            average_traded_price
        )

        cumulative_pv = atp * cumulative

        if (
            not math.isfinite(cumulative_pv)
            or cumulative_pv <= 0
        ):
            return False

        self.cum_vol = cumulative
        self.cum_pv = cumulative_pv
        self.current_vwap = atp
        self.vwap_source = "KITE_ATP"

        return True

    def _update_observed_vwap(
        self,
        price: float,
        volume: int,
    ) -> None:
        """
        Fallback VWAP using only observed incremental traded volume.

        This is used only when Kite ATP/cumulative volume is unavailable.
        """
        if volume <= 0:
            return

        if not math.isfinite(price) or price <= 0:
            return

        self.cum_pv += price * volume
        self.cum_vol += volume

        if self.cum_vol <= 0:
            return

        self.current_vwap = (
            self.cum_pv / self.cum_vol
        )
        self.vwap_source = "OBSERVED_TICKS"

    # ================================================================
    # CANDLE FINALIZATION
    # ================================================================

    def _finalize_current_candle_locked(
        self,
    ) -> Optional[Tuple[dict, float]]:
        """
        Finalize the current candle.

        IMPORTANT:
        The VWAP is captured BEFORE the incoming boundary tick is allowed
        to modify the session VWAP.

        This is the critical look-ahead fix.
        """
        candle = self.current_candle

        if candle is None or candle.open is None:
            return None

        candle.finalize()

        completed_vwap = float(
            self.current_vwap
        )

        payload = candle.to_dict()

        payload["vwap"] = (
            completed_vwap
            if (
                completed_vwap > 0
                and math.isfinite(completed_vwap)
            )
            else None
        )

        payload["vwap_source"] = (
            self.vwap_source
        )

        self.completed_candles.append(
            payload
        )

        self.current_candle = None

        return payload, completed_vwap

    def _emit_callback(
        self,
        payload: Optional[Tuple[dict, float]],
    ) -> None:
        """
        Execute the external candle-close callback outside the lock.
        """
        if payload is None:
            return

        candle_dict, completed_vwap = payload

        if (
            completed_vwap <= 0
            or not math.isfinite(completed_vwap)
        ):
            logger.warning(
                "[%s] Completed candle %s has no valid VWAP; "
                "candle retained but strategy callback skipped.",
                self.symbol,
                candle_dict.get("datetime"),
            )
            return

        callback = self.on_candle_close

        if callback is None:
            return

        try:
            callback(
                candle_dict,
                completed_vwap,
            )
        except Exception:
            logger.exception(
                "[%s] Candle-close callback failed "
                "for %s",
                self.symbol,
                candle_dict.get("datetime"),
            )

    # ================================================================
    # SINGLE TICK
    # ================================================================

    def process_tick(
        self,
        price: float,
        volume: int,
        timestamp: datetime,
        average_traded_price: Optional[float] = None,
        cumulative_volume: Optional[int] = None,
    ) -> None:
        """
        Process one tick.

        Parameters
        ----------
        price:
            Last traded price.

        volume:
            Incremental traded quantity for this tick.

        timestamp:
            Exchange timestamp.

        average_traded_price:
            Kite's running session average traded price.

        cumulative_volume:
            Kite's running session traded volume.

        Boundary order
        --------------
        1. Detect candle boundary.
        2. Finalize old candle.
        3. Capture old VWAP.
        4. Start new candle.
        5. Add new tick.
        6. Update session VWAP with the new tick.

        This ordering prevents the first tick of the next candle from
        contaminating the completed candle's VWAP.
        """
        normalized_ts = _normalize_ist_naive(
            timestamp
        )

        if normalized_ts is None:
            logger.warning(
                "[%s] Dropping tick with invalid timestamp: %r",
                self.symbol,
                timestamp,
            )
            return

        try:
            px = float(price)
        except (TypeError, ValueError):
            logger.warning(
                "[%s] Dropping tick with invalid price: %r",
                self.symbol,
                price,
            )
            return

        if (
            not math.isfinite(px)
            or px <= 0
        ):
            logger.warning(
                "[%s] Dropping tick with invalid price: %r",
                self.symbol,
                price,
            )
            return

        try:
            vol = int(volume)
        except (TypeError, ValueError):
            vol = 0

        if vol < 0:
            vol = 0

        callback_payload = None

        with self._lock:
            if not self._ensure_session_locked(
                normalized_ts
            ):
                return

            if (
                self.last_tick_timestamp is not None
                and normalized_ts < self.last_tick_timestamp
            ):
                logger.warning(
                    "[%s] Dropping out-of-order tick "
                    "%s < %s",
                    self.symbol,
                    normalized_ts,
                    self.last_tick_timestamp,
                )
                return

            self.last_tick_timestamp = normalized_ts

            candle_start = self._candle_start(
                normalized_ts
            )

            # --------------------------------------------------------
            # Market close boundary
            # --------------------------------------------------------
            if normalized_ts.time() >= self.session_close:
                if self.current_candle is not None:
                    callback_payload = (
                        self._finalize_current_candle_locked()
                    )

            else:
                # ----------------------------------------------------
                # New candle boundary
                # ----------------------------------------------------
                if (
                    self.current_candle is not None
                    and candle_start
                    >= self.current_candle.end_time
                ):
                    callback_payload = (
                        self._finalize_current_candle_locked()
                    )

                # ----------------------------------------------------
                # Create current candle
                # ----------------------------------------------------
                if self.current_candle is None:
                    self.current_candle = Candle(
                        symbol=self.symbol,
                        start_time=candle_start,
                        timeframe_minutes=self.timeframe_minutes,
                    )

                # ----------------------------------------------------
                # Add the current tick to the CURRENT candle.
                # ----------------------------------------------------
                self.current_candle.update(
                    price=px,
                    volume=vol,
                )

                # ----------------------------------------------------
                # Update VWAP AFTER boundary callback state has been
                # captured.
                # ----------------------------------------------------
                exchange_vwap_ok = False

                if (
                    average_traded_price is not None
                    and cumulative_volume is not None
                ):
                    exchange_vwap_ok = (
                        self._set_exchange_vwap(
                            average_traded_price,
                            cumulative_volume,
                        )
                    )

                if not exchange_vwap_ok:
                    self._update_observed_vwap(
                        price=px,
                        volume=vol,
                    )

        # Never hold the aggregation lock while executing the strategy
        # callback.
        self._emit_callback(
            callback_payload
        )

    # ================================================================
    # READ APIs
    # ================================================================

    def get_completed_dataframe(
        self,
    ) -> pd.DataFrame:
        with self._lock:
            if not self.completed_candles:
                return pd.DataFrame()

            rows = list(
                self.completed_candles
            )

        df = pd.DataFrame(rows)

        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(
                df["datetime"]
            )

        if "end_time" in df.columns:
            df["end_time"] = pd.to_datetime(
                df["end_time"]
            )

        return df

    def get_current_vwap(
        self,
    ) -> float:
        with self._lock:
            return float(
                self.current_vwap
            )

    def get_current_candle(
        self,
    ) -> Optional[dict]:
        with self._lock:
            if self.current_candle is None:
                return None

            return self.current_candle.to_dict()


class MultiSymbolCandleAggregator:
    """
    Multi-symbol dispatcher.

    Data structures
    ---------------
    token_to_symbol_map:
        token -> symbol
        O(1) average lookup

    symbol_to_token:
        symbol -> token
        O(1) average lookup

    aggregators:
        symbol -> CandleAggregator
        O(1) average lookup

    latest_book_snapshots:
        symbol -> most recent L5 snapshot
        O(1) average lookup

    last_volume_by_token:
        token -> latest cumulative volume
        O(1) average update
    """

    def __init__(
        self,
        token_to_symbol_map: Dict[int, str],
        timeframe_minutes: int = 15,
        on_candle_close: Optional[
            Callable[[dict, float], None]
        ] = None,
        on_book_update: Optional[
            Callable[[str, Any], None]
        ] = None,
        max_completed_candles_per_symbol: int = 512,
    ):
        if not token_to_symbol_map:
            raise ValueError(
                "token_to_symbol_map must contain at least one instrument"
            )

        if timeframe_minutes <= 0:
            raise ValueError(
                "timeframe_minutes must be > 0"
            )

        if max_completed_candles_per_symbol <= 0:
            raise ValueError(
                "max_completed_candles_per_symbol must be > 0"
            )

        self.token_to_symbol_map: Dict[int, str] = {}
        self.symbol_to_token: Dict[str, int] = {}

        # ------------------------------------------------------------
        # Validate the supplied token map.
        # ------------------------------------------------------------
        for raw_token, raw_symbol in (
            token_to_symbol_map.items()
        ):
            try:
                token = int(raw_token)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid instrument token: {raw_token!r}"
                ) from exc

            if token <= 0:
                raise ValueError(
                    f"Instrument token must be > 0: {token}"
                )

            symbol = str(
                raw_symbol
            ).strip().upper()

            if not symbol:
                raise ValueError(
                    f"Instrument symbol is empty for token {token}"
                )

            previous_symbol = (
                self.token_to_symbol_map.get(token)
            )

            if (
                previous_symbol is not None
                and previous_symbol != symbol
            ):
                raise ValueError(
                    f"Token {token} maps to multiple symbols: "
                    f"{previous_symbol!r} and {symbol!r}"
                )

            previous_token = (
                self.symbol_to_token.get(symbol)
            )

            if (
                previous_token is not None
                and previous_token != token
            ):
                raise ValueError(
                    f"Symbol {symbol!r} maps to multiple tokens: "
                    f"{previous_token} and {token}"
                )

            self.token_to_symbol_map[token] = symbol
            self.symbol_to_token[symbol] = token

        self.timeframe_minutes = int(
            timeframe_minutes
        )

        self.on_candle_close = (
            on_candle_close
        )

        self.on_book_update = (
            on_book_update
        )

        self.max_completed_candles_per_symbol = (
            int(max_completed_candles_per_symbol)
        )

        self.aggregators: Dict[
            str,
            CandleAggregator,
        ] = {}

        self.latest_book_snapshots: Dict[
            str,
            Any,
        ] = {}

        self.last_volume_by_token: Dict[
            int,
            int,
        ] = {}

        self.volume_session_date_by_token: Dict[
            int,
            date,
        ] = {}

        self.last_tick_timestamp_by_token: Dict[
            int,
            datetime,
        ] = {}

        self._lock = threading.RLock()

        self._init_aggregators()

    # ================================================================
    # INITIALIZATION
    # ================================================================

    def _init_aggregators(
        self,
    ) -> None:
        with self._lock:
            for symbol in self.symbol_to_token:
                self.aggregators[symbol] = (
                    CandleAggregator(
                        symbol=symbol,
                        timeframe_minutes=self.timeframe_minutes,
                        on_candle_close=self.on_candle_close,
                        max_completed_candles=(
                            self.max_completed_candles_per_symbol
                        ),
                    )
                )

    # ================================================================
    # VOLUME DELTA
    # ================================================================

    def _calculate_incremental_volume(
        self,
        token: int,
        timestamp: datetime,
        cumulative_volume: Optional[int],
        last_traded_quantity: Optional[int],
    ) -> int:
        """
        Convert Kite cumulative daily volume into candle volume.

        Rules
        -----
        First observed tick:
            count only its last trade quantity when available.

        Later ticks:
            cumulative_volume_delta.

        This avoids:
            - assigning the entire daily volume to one candle
            - double-counting repeated ticks
            - negative candle volume after a counter reset
        """
        if cumulative_volume is None:
            return (
                last_traded_quantity or 0
            )

        session_date = timestamp.date()

        previous_session = (
            self.volume_session_date_by_token.get(
                token
            )
        )

        # New trading day.
        if previous_session != session_date:
            self.volume_session_date_by_token[
                token
            ] = session_date

            self.last_volume_by_token[
                token
            ] = cumulative_volume

            if last_traded_quantity is not None:
                return min(
                    last_traded_quantity,
                    cumulative_volume,
                )

            return 0

        previous_volume = (
            self.last_volume_by_token.get(
                token
            )
        )

        # First tick observed after object creation/restart.
        if previous_volume is None:
            self.last_volume_by_token[
                token
            ] = cumulative_volume

            if last_traded_quantity is not None:
                return min(
                    last_traded_quantity,
                    cumulative_volume,
                )

            return 0

        # Cumulative volume should never decrease during one trading day.
        if cumulative_volume < previous_volume:
            logger.warning(
                "Cumulative volume decreased for token %s: "
                "%s -> %s. Re-baselining.",
                token,
                previous_volume,
                cumulative_volume,
            )

            self.last_volume_by_token[
                token
            ] = cumulative_volume

            if last_traded_quantity is not None:
                return min(
                    last_traded_quantity,
                    cumulative_volume,
                )

            return 0

        delta = (
            cumulative_volume
            - previous_volume
        )

        self.last_volume_by_token[
            token
        ] = cumulative_volume

        return max(
            delta,
            0,
        )

    # ================================================================
    # LEVEL-5
    # ================================================================

    @staticmethod
    def _parse_depth_snapshot(
        tick: Dict[str, Any],
        timestamp: datetime,
        price: float,
    ) -> Optional[Any]:
        """
        Extract the top 5 buy/sell levels from Kite FULL mode.

        Returns None when depth is unavailable or invalid.
        """
        depth = tick.get("depth")

        if not isinstance(depth, dict):
            return None

        buy_levels = depth.get(
            "buy"
        )

        sell_levels = depth.get(
            "sell"
        )

        if not isinstance(
            buy_levels,
            list,
        ):
            return None

        if not isinstance(
            sell_levels,
            list,
        ):
            return None

        if (
            len(buy_levels) < 5
            or len(sell_levels) < 5
        ):
            return None

        try:
            bids = []
            asks = []

            # Only top 5 are required by SSF-L5-SRM.
            for level in buy_levels[:5]:
                if not isinstance(
                    level,
                    dict,
                ):
                    raise ValueError(
                        "Invalid bid level"
                    )

                bid_price = float(
                    level["price"]
                )
                bid_qty = int(
                    level["quantity"]
                )
                bid_orders = int(
                    level.get(
                        "orders",
                        1,
                    )
                )

                if (
                    not math.isfinite(
                        bid_price
                    )
                    or bid_price <= 0
                    or bid_qty < 0
                    or bid_orders < 0
                ):
                    raise ValueError(
                        "Invalid bid values"
                    )

                bids.append(
                    (
                        bid_price,
                        bid_qty,
                        bid_orders,
                    )
                )

            for level in sell_levels[:5]:
                if not isinstance(
                    level,
                    dict,
                ):
                    raise ValueError(
                        "Invalid ask level"
                    )

                ask_price = float(
                    level["price"]
                )
                ask_qty = int(
                    level["quantity"]
                )
                ask_orders = int(
                    level.get(
                        "orders",
                        1,
                    )
                )

                if (
                    not math.isfinite(
                        ask_price
                    )
                    or ask_price <= 0
                    or ask_qty < 0
                    or ask_orders < 0
                ):
                    raise ValueError(
                        "Invalid ask values"
                    )

                asks.append(
                    (
                        ask_price,
                        ask_qty,
                        ask_orders,
                    )
                )

            from strategy.ssf_l5_srm_strategy import (
                BookSnapshot,
            )

            oi_raw = tick.get("oi")
            lower_raw = tick.get(
                "lower_circuit_limit"
            )
            upper_raw = tick.get(
                "upper_circuit_limit"
            )

            oi = (
                float(oi_raw)
                if _is_finite_positive(oi_raw)
                else None
            )

            circuit_lower = (
                float(lower_raw)
                if _is_finite_positive(lower_raw)
                else None
            )

            circuit_upper = (
                float(upper_raw)
                if _is_finite_positive(upper_raw)
                else None
            )

            return BookSnapshot(
                timestamp=timestamp,
                bids=bids,
                asks=asks,
                ltp=price,
                fut_ltp=None,
                fut_oi=oi,
                circuit_lower=circuit_lower,
                circuit_upper=circuit_upper,
            )

        except (
            KeyError,
            TypeError,
            ValueError,
            ImportError,
        ):
            logger.exception(
                "Invalid Level-5 depth received "
                "for token %s",
                tick.get("instrument_token"),
            )
            return None

    # ================================================================
    # RAW KITE TICK BATCH
    # ================================================================

    def process_ticks(
        self,
        ticks: Any,
    ) -> None:
        """
        Process a raw KiteTicker callback batch.

        Expected input:
            list[dict]

        A single dict is also accepted.
        """
        if isinstance(ticks, dict):
            tick_batch = [ticks]
        elif isinstance(ticks, list):
            tick_batch = ticks
        else:
            return

        for tick in tick_batch:
            if not isinstance(
                tick,
                dict,
            ):
                continue

            raw_token = tick.get(
                "instrument_token"
            )

            try:
                token = int(
                    raw_token
                )
            except (TypeError, ValueError):
                logger.warning(
                    "Dropping tick with invalid instrument_token: %r",
                    raw_token,
                )
                continue

            # --------------------------------------------------------
            # O(1) token -> symbol lookup
            # --------------------------------------------------------
            with self._lock:
                symbol = (
                    self.token_to_symbol_map.get(
                        token
                    )
                )

            if symbol is None:
                logger.warning(
                    "Ignoring tick for unsubscribed token %s",
                    token,
                )
                continue

            # --------------------------------------------------------
            # Validate last price
            # --------------------------------------------------------
            raw_price = tick.get(
                "last_price"
            )

            if not _is_finite_positive(
                raw_price
            ):
                logger.warning(
                    "[%s] Dropping tick with invalid last_price=%r",
                    symbol,
                    raw_price,
                )
                continue

            price = float(
                raw_price
            )

            # --------------------------------------------------------
            # Exchange timestamp.
            #
            # NEVER fall back to server clock.
            # --------------------------------------------------------
            raw_timestamp = (
                tick.get(
                    "exchange_timestamp"
                )
                or tick.get(
                    "timestamp"
                )
            )

            timestamp = _normalize_ist_naive(
                raw_timestamp
            )

            if timestamp is None:
                logger.warning(
                    "[%s] Dropping tick because exchange timestamp "
                    "is missing or invalid.",
                    symbol,
                )
                continue

            # --------------------------------------------------------
            # Kite volume fields.
            # --------------------------------------------------------
            cumulative_volume = (
                _safe_non_negative_int(
                    tick.get(
                        "volume_traded"
                    )
                )
            )

            raw_last_qty = (
                tick.get(
                    "last_traded_quantity"
                )
            )

            if raw_last_qty is None:
                raw_last_qty = tick.get(
                    "last_quantity"
                )

            last_traded_quantity = (
                _safe_non_negative_int(
                    raw_last_qty
                )
            )

            # --------------------------------------------------------
            # Validate tick ordering and calculate candle volume.
            # --------------------------------------------------------
            with self._lock:
                previous_timestamp = (
                    self.last_tick_timestamp_by_token.get(
                        token
                    )
                )

                if (
                    previous_timestamp is not None
                    and timestamp < previous_timestamp
                ):
                    logger.warning(
                        "[%s] Dropping out-of-order tick "
                        "%s < %s",
                        symbol,
                        timestamp,
                        previous_timestamp,
                    )
                    continue

                self.last_tick_timestamp_by_token[
                    token
                ] = timestamp

                incremental_volume = (
                    self._calculate_incremental_volume(
                        token=token,
                        timestamp=timestamp,
                        cumulative_volume=cumulative_volume,
                        last_traded_quantity=last_traded_quantity,
                    )
                )

                aggregator = (
                    self.aggregators.get(
                        symbol
                    )
                )

            if aggregator is None:
                continue

            # --------------------------------------------------------
            # Exchange-provided running ATP.
            # --------------------------------------------------------
            average_traded_price = tick.get(
                "average_traded_price"
            )

            # --------------------------------------------------------
            # Candle processing.
            # The aggregator fixes the candle/VWAP boundary ordering.
            # --------------------------------------------------------
            aggregator.process_tick(
                price=price,
                volume=incremental_volume,
                timestamp=timestamp,
                average_traded_price=(
                    average_traded_price
                ),
                cumulative_volume=(
                    cumulative_volume
                ),
            )

            # --------------------------------------------------------
            # Level-5 order book.
            # --------------------------------------------------------
            snapshot = (
                self._parse_depth_snapshot(
                    tick=tick,
                    timestamp=timestamp,
                    price=price,
                )
            )

            if snapshot is None:
                continue

            callback = None

            with self._lock:
                self.latest_book_snapshots[
                    symbol
                ] = snapshot

                callback = self.on_book_update

            # Never call user code while holding self._lock.
            if callback is not None:
                try:
                    callback(
                        symbol,
                        snapshot,
                    )
                except Exception:
                    logger.exception(
                        "[%s] on_book_update callback failed",
                        symbol,
                    )

    # ================================================================
    # READ APIs
    # ================================================================

    def get_symbol_dataframe(
        self,
        symbol: str,
    ) -> pd.DataFrame:
        symbol_key = str(
            symbol
        ).strip().upper()

        with self._lock:
            aggregator = (
                self.aggregators.get(
                    symbol_key
                )
            )

        if aggregator is None:
            return pd.DataFrame()

        return aggregator.get_completed_dataframe()

    def get_latest_book_snapshot(
        self,
        symbol: str,
    ) -> Optional[Any]:
        symbol_key = str(
            symbol
        ).strip().upper()

        with self._lock:
            return (
                self.latest_book_snapshots.get(
                    symbol_key
                )
            )

    def get_current_vwap(
        self,
        symbol: str,
    ) -> float:
        symbol_key = str(
            symbol
        ).strip().upper()

        with self._lock:
            aggregator = (
                self.aggregators.get(
                    symbol_key
                )
            )

        if aggregator is None:
            return 0.0

        return aggregator.get_current_vwap()

    def get_current_candle(
        self,
        symbol: str,
    ) -> Optional[dict]:
        symbol_key = str(
            symbol
        ).strip().upper()

        with self._lock:
            aggregator = (
                self.aggregators.get(
                    symbol_key
                )
            )

        if aggregator is None:
            return None

        return aggregator.get_current_candle()

    # ================================================================
    # DAILY RESET
    # ================================================================

    def reset_all_daily_sessions(
        self,
    ) -> None:
        """
        Reset all per-symbol intraday state.
        """
        with self._lock:
            for aggregator in (
                self.aggregators.values()
            ):
                aggregator.reset_daily_session()

            self.last_volume_by_token.clear()
            self.volume_session_date_by_token.clear()
            self.last_tick_timestamp_by_token.clear()
            self.latest_book_snapshots.clear()