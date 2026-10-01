"""
Central Pivot Range (CPR) Regime Strategy.

Rules
-----
1. CPR is calculated from the PRIOR completed trading session:
       P  = (H + L + C) / 3
       BC = (H + L) / 2
       TC = 2P - BC

2. CPR boundaries are normalized:
       cpr_bottom = min(raw_BC, raw_TC)
       cpr_top    = max(raw_BC, raw_TC)

3. CPR width is ranked against a trailing distribution of prior-session
   CPR widths:
       <= 20th percentile -> NARROW
       >= 80th percentile -> WIDE
       otherwise           -> NEUTRAL

4. NARROW regime:
       LONG  -> completed candle closes above CPR top
       SHORT -> completed candle closes below CPR bottom

5. WIDE regime:
       LONG  -> candle tests CPR bottom and closes back above it
       SHORT -> candle tests CPR top and closes back below it

6. All entries obey the configured entry window.

7. Every generated trade must satisfy:
       LONG:  stop < entry < target
       SHORT: target < entry < stop

8. Maximum stop-distance risk is enforced before emitting a signal.

9. Breakeven is activated at configured R multiple.

10. Candle timestamps are treated as candle-open timestamps by default,
    matching Zerodha historical 15-minute candle semantics.
"""

from datetime import date, datetime, time
from typing import Dict, List, Optional

import pandas as pd

from config.settings import InstrumentConfig, InstrumentType, StrategyConfig, settings
from monitoring.logger import logger
from strategy.base_strategy import BaseStrategy, SignalAction, StrategySignal


class Regime:
    NARROW = "NARROW"
    WIDE = "WIDE"
    NEUTRAL = "NEUTRAL"


def _normalize_ist_naive(value) -> datetime:
    """
    Normalize any timestamp to Asia/Kolkata and return a naive datetime.

    Naive timestamps are assumed to already represent IST.
    """
    ts = pd.Timestamp(value)

    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)

    return ts.to_pydatetime()


def _compute_pivots(high: float, low: float, close: float) -> Dict[str, float]:
    """
    Calculate raw CPR/pivot values from one completed prior session.
    """
    high = float(high)
    low = float(low)
    close = float(close)

    if not (low <= high):
        raise ValueError(f"Invalid OHLC: low={low}, high={high}")

    p = (high + low + close) / 3.0
    raw_bc = (high + low) / 2.0
    raw_tc = (2.0 * p) - raw_bc

    # Critical normalization:
    # Depending on the prior close, raw TC can be below raw BC.
    # The actual CPR range must always be represented bottom -> top.
    cpr_bottom = min(raw_bc, raw_tc)
    cpr_top = max(raw_bc, raw_tc)

    r1 = (2.0 * p) - low
    s1 = (2.0 * p) - high

    width = abs(cpr_top - cpr_bottom)
    width_pct = (width / p) * 100.0 if p > 0 else 0.0

    return {
        "P": p,
        "BC": raw_bc,
        "TC": raw_tc,
        "cpr_bottom": cpr_bottom,
        "cpr_top": cpr_top,
        "R1": r1,
        "S1": s1,
        "width": width,
        "width_pct": width_pct,
        "prior_high": high,
        "prior_low": low,
        "prior_close": close,
    }


class CPRRegimeBreakoutStrategy(BaseStrategy):
    def __init__(
        self,
        instrument: InstrumentConfig,
        strategy_config: StrategyConfig = settings.strategy,
        entry_window_start: Optional[time] = None,
        entry_window_end: Optional[time] = None,
        width_lookback_days: int = 20,
        candle_timestamps_are_open: bool = True,
        execution_policy: str = "CONSERVATIVE",
    ):
        super().__init__(instrument.symbol)

        self.instrument = instrument
        self.config = strategy_config

        self.entry_window_start = (
            entry_window_start
            if entry_window_start is not None
            else getattr(self.config, "entry_start", time(9, 45))
        )

        self.entry_window_end = (
            entry_window_end
            if entry_window_end is not None
            else getattr(self.config, "entry_end", time(13, 30))
        )

        self.width_lookback_days = max(5, int(width_lookback_days))
        self.candle_timestamps_are_open = bool(candle_timestamps_are_open)

        self.execution_policy = getattr(execution_policy, "value", execution_policy)
        self.execution_policy = str(self.execution_policy).upper()

        self.current_date: Optional[date] = None
        self.pivots: Optional[Dict[str, float]] = None
        self.regime: str = Regime.NEUTRAL
        self.width_percentile: Optional[float] = None

        # Raw completed intraday candles for the current session.
        self.history_today: List[dict] = []

    # ------------------------------------------------------------------
    # Session / timestamp helpers
    # ------------------------------------------------------------------

    def _event_timestamp(self, raw_timestamp) -> datetime:
        """
        Convert candle-open timestamp -> candle-completion timestamp.

        Zerodha 15m example:
            09:15 stamp = 09:15 -> 09:30
            09:30 stamp = 09:30 -> 09:45
        """
        ts = _normalize_ist_naive(raw_timestamp)

        if self.candle_timestamps_are_open:
            minutes = int(getattr(self.config, "candle_timeframe_minutes", 15))
            ts += pd.Timedelta(minutes=minutes).to_pytimedelta()

        return ts

    def reset_session(self, session_date: date):
        self.current_date = session_date
        self.history_today.clear()

        # CPR itself comes from prior-session context and is intentionally
        # preserved across reset_session() calls.
        #
        # Per-session trade state must always reset.
        self.position = 0
        self.entry_price = 0.0
        self.stop_loss = 0.0
        self.target = 0.0
        self.initial_risk_dist = 0.0
        self.trailing_breakeven_active = False
        self.trades_today = 0

        logger.info(f"[{self.symbol}] CPR session reset for {session_date}.")

    def register_trade_entry(
        self,
        entry_price: float,
        position: int,
        stop_loss: float,
        target: float,
        risk_dist: float,
    ):
        """
        Fill-confirmation hook.
        """
        super().register_trade_entry(
            entry_price=float(entry_price),
            position=int(position),
            stop_loss=float(stop_loss),
            target=float(target),
            risk_dist=float(risk_dist),
        )

    def register_trade_exit(self):
        """
        Exit-confirmation hook.
        """
        super().register_trade_exit()

    # ------------------------------------------------------------------
    # CPR context
    # ------------------------------------------------------------------

    def seed_context(self, historical_bars: pd.DataFrame) -> None:
        """
        Build today's CPR from the most recent completed prior session and
        determine today's width regime from prior-session CPR widths.

        historical_bars should contain ONLY data before today's session.
        """
        self.pivots = None
        self.regime = Regime.NEUTRAL
        self.width_percentile = None

        if historical_bars is None or historical_bars.empty:
            logger.warning(f"[{self.symbol}] CPR: no historical context.")
            return

        required = {"datetime", "high", "low", "close"}
        missing = required - set(historical_bars.columns)

        if missing:
            logger.warning(
                f"[{self.symbol}] CPR: missing context columns: {sorted(missing)}"
            )
            return

        data = historical_bars.copy()

        try:
            data["datetime"] = data["datetime"].map(_normalize_ist_naive)
            data["high"] = pd.to_numeric(data["high"], errors="coerce")
            data["low"] = pd.to_numeric(data["low"], errors="coerce")
            data["close"] = pd.to_numeric(data["close"], errors="coerce")

            data = data.dropna(subset=["datetime", "high", "low", "close"])
            data = data[data["low"] <= data["high"]]
            data = data.sort_values("datetime")
        except Exception as exc:
            logger.warning(
                f"[{self.symbol}] CPR context normalization failed: {exc}"
            )
            return

        if data.empty:
            return

        data["date"] = data["datetime"].map(lambda x: x.date())

        daily = (
            data.groupby("date", sort=True)
            .agg(
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
            )
            .sort_index()
        )

        if daily.empty:
            return

        pivot_rows: List[Dict[str, float]] = []

        for session_date, row in daily.iterrows():
            try:
                pivots = _compute_pivots(
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                )
                pivots["session_date"] = session_date
                pivot_rows.append(pivots)
            except (TypeError, ValueError):
                continue

        if not pivot_rows:
            return

        # Most recent completed prior session defines today's CPR.
        self.pivots = dict(pivot_rows[-1])

        widths = [
            float(item["width_pct"])
            for item in pivot_rows[-self.width_lookback_days:]
            if float(item["width_pct"]) >= 0.0
        ]

        if len(widths) < 5:
            self.regime = Regime.NEUTRAL
            self.width_percentile = None
        else:
            today_width = widths[-1]
            width_series = pd.Series(widths, dtype=float)

            # Empirical percentile rank with average tie handling.
            # This avoids the old strict "<" tie bug.
            self.width_percentile = float(
                width_series.rank(pct=True, method="average").iloc[-1] * 100.0
            )

            p20 = float(width_series.quantile(0.20))
            p80 = float(width_series.quantile(0.80))

            if today_width <= p20:
                self.regime = Regime.NARROW
            elif today_width >= p80:
                self.regime = Regime.WIDE
            else:
                self.regime = Regime.NEUTRAL

        logger.info(
            f"[{self.symbol}] CPR context: "
            f"P={self.pivots['P']:.2f} "
            f"raw_BC={self.pivots['BC']:.2f} "
            f"raw_TC={self.pivots['TC']:.2f} "
            f"bottom={self.pivots['cpr_bottom']:.2f} "
            f"top={self.pivots['cpr_top']:.2f} "
            f"width={self.pivots['width_pct']:.2f}% "
            f"percentile={self.width_percentile} "
            f"regime={self.regime}"
        )

    # ------------------------------------------------------------------
    # Risk / validation helpers
    # ------------------------------------------------------------------

    def _max_allowed_risk(self, entry_price: float) -> float:
        """
        Hard stop-distance cap.

        Equities:
            percentage-based risk so the cap scales with stock price.

        Non-equities:
            configured absolute max_risk_cap.
        """
        entry_price = float(entry_price)

        if entry_price <= 0:
            return 0.0

        tick_size = max(float(getattr(self.instrument, "tick_size", 0.05)), 0.000001)

        if self.instrument.instrument_type == InstrumentType.EQUITY:
            pct = self.instrument.equity_orb_max_risk_pct
            if pct is None or pct <= 0:
                pct = 0.0040
            return max(entry_price * float(pct), tick_size)

        configured_cap = float(
            getattr(self.instrument, "max_risk_cap", 0.0)
        )
        return max(configured_cap, tick_size)

    def _valid_long_trade(
        self,
        entry: float,
        stop: float,
        target: float,
    ) -> bool:
        """
        Strict long structure:
            stop < entry < target
        """
        tick_size = max(float(getattr(self.instrument, "tick_size", 0.05)), 0.000001)

        return (
            stop < (entry - tick_size * 0.01)
            and target > (entry + tick_size * 0.01)
        )

    def _valid_short_trade(
        self,
        entry: float,
        stop: float,
        target: float,
    ) -> bool:
        """
        Strict short structure:
            target < entry < stop
        """
        tick_size = max(float(getattr(self.instrument, "tick_size", 0.05)), 0.000001)

        return (
            target < (entry - tick_size * 0.01)
            and stop > (entry + tick_size * 0.01)
        )

    def _risk_allowed(
        self,
        entry: float,
        stop: float,
    ) -> bool:
        risk = abs(float(entry) - float(stop))

        if risk <= 0:
            return False

        max_risk = self._max_allowed_risk(entry)

        if risk > max_risk:
            logger.info(
                f"[{self.symbol}] CPR trade rejected: "
                f"risk={risk:.4f} > max_allowed={max_risk:.4f}"
            )
            return False

        return True

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    def _generate_entry_signal(
        self,
        candle: dict,
    ) -> Optional[StrategySignal]:
        if self.pivots is None:
            return None

        if self.regime == Regime.NEUTRAL:
            return None

        if self.position != 0 or self.trades_today != 0:
            return None

        timestamp = _normalize_ist_naive(candle["datetime"])
        bar_time = timestamp.time()

        # IMPORTANT:
        # Both NARROW and WIDE regimes must obey the same entry window.
        if not (
            self.entry_window_start
            <= bar_time
            <= self.entry_window_end
        ):
            return None

        p = float(self.pivots["P"])
        cpr_bottom = float(self.pivots["cpr_bottom"])
        cpr_top = float(self.pivots["cpr_top"])
        prior_range = float(
            self.pivots["prior_high"] - self.pivots["prior_low"]
        )

        close = float(candle["close"])
        high = float(candle["high"])
        low = float(candle["low"])

        # --------------------------------------------------------------
        # NARROW = breakout
        # --------------------------------------------------------------

        if self.regime == Regime.NARROW:
            # LONG: close above the top of the normalized CPR range.
            if close > cpr_top:
                stop = p
                target = p + prior_range
                risk = close - stop

                if not self._valid_long_trade(close, stop, target):
                    logger.info(
                        f"[{self.symbol}] CPR narrow LONG rejected: "
                        f"invalid levels entry={close:.2f}, "
                        f"stop={stop:.2f}, target={target:.2f}"
                    )
                    return None

                if not self._risk_allowed(close, stop):
                    return None

                return StrategySignal(
                    action=SignalAction.BUY,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=close,
                    stop_loss=stop,
                    target=target,
                    reason="CPR_NARROW_BREAKOUT_LONG",
                )

            # SHORT: close below the bottom of the normalized CPR range.
            if close < cpr_bottom:
                stop = p
                target = p - prior_range
                risk = stop - close

                if not self._valid_short_trade(close, stop, target):
                    logger.info(
                        f"[{self.symbol}] CPR narrow SHORT rejected: "
                        f"invalid levels entry={close:.2f}, "
                        f"stop={stop:.2f}, target={target:.2f}"
                    )
                    return None

                if not self._risk_allowed(close, stop):
                    return None

                return StrategySignal(
                    action=SignalAction.SELL,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=close,
                    stop_loss=stop,
                    target=target,
                    reason="CPR_NARROW_BREAKDOWN_SHORT",
                )

            return None

        # --------------------------------------------------------------
        # WIDE = mean reversion
        # --------------------------------------------------------------

        if self.regime == Regime.WIDE:
            # LONG:
            # Price probes the lower CPR boundary but closes back above it.
            if low <= cpr_bottom and close > cpr_bottom:
                distance_from_p = abs(p - cpr_bottom)

                stop = cpr_bottom - distance_from_p
                target = cpr_top
                risk = close - stop

                if not self._valid_long_trade(close, stop, target):
                    logger.info(
                        f"[{self.symbol}] CPR wide LONG rejected: "
                        f"invalid levels entry={close:.2f}, "
                        f"stop={stop:.2f}, target={target:.2f}"
                    )
                    return None

                if not self._risk_allowed(close, stop):
                    return None

                return StrategySignal(
                    action=SignalAction.BUY,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=close,
                    stop_loss=stop,
                    target=target,
                    reason="CPR_WIDE_FADE_LONG_AT_BOTTOM",
                )

            # SHORT:
            # Price probes the upper CPR boundary but closes back below it.
            if high >= cpr_top and close < cpr_top:
                distance_from_p = abs(cpr_top - p)

                stop = cpr_top + distance_from_p
                target = cpr_bottom
                risk = stop - close

                if not self._valid_short_trade(close, stop, target):
                    logger.info(
                        f"[{self.symbol}] CPR wide SHORT rejected: "
                        f"invalid levels entry={close:.2f}, "
                        f"stop={stop:.2f}, target={target:.2f}"
                    )
                    return None

                if not self._risk_allowed(close, stop):
                    return None

                return StrategySignal(
                    action=SignalAction.SELL,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=close,
                    stop_loss=stop,
                    target=target,
                    reason="CPR_WIDE_FADE_SHORT_AT_TOP",
                )

        return None

    # ------------------------------------------------------------------
    # Candle processing
    # ------------------------------------------------------------------

    def on_candle(
        self,
        candle: dict,
        vwap: float,
    ) -> Optional[StrategySignal]:
        """
        Process one COMPLETED intraday candle.

        Raw input timestamp is normally the candle OPEN timestamp.
        Schedule/exit logic uses the candle COMPLETION timestamp.
        """
        raw_timestamp = _normalize_ist_naive(candle["datetime"])
        event_timestamp = self._event_timestamp(raw_timestamp)

        try:
            working_candle = {
                "datetime": event_timestamp,
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
                "volume": int(candle.get("volume", 0)),
            }
        except (TypeError, ValueError, KeyError) as exc:
            logger.warning(
                f"[{self.symbol}] CPR invalid candle: {exc}"
            )
            return None

        # Detect session transition using the actual event/completion time.
        if self.current_date is None:
            self.reset_session(event_timestamp.date())
        elif event_timestamp.date() != self.current_date:
            self.reset_session(event_timestamp.date())

        # Preserve raw candle-open timestamp for diagnostics/history.
        history_candle = dict(working_candle)
        history_candle["datetime"] = raw_timestamp
        self.history_today.append(history_candle)

        # --------------------------------------------------------------
        # 1. Mandatory square-off
        # --------------------------------------------------------------

        if (
            event_timestamp.time() >= self.config.square_off_time
            and self.position != 0
        ):
            return StrategySignal(
                action=SignalAction.EXIT,
                symbol=self.symbol,
                timestamp=event_timestamp,
                price=working_candle["close"],
                reason="TIME_SQUARE_OFF",
            )

        # --------------------------------------------------------------
        # 2. Manage current position
        # --------------------------------------------------------------

        if self.position != 0:
            exit_signal = self._check_active_position_exits(
                working_candle
            )

            if exit_signal:
                return exit_signal

        # --------------------------------------------------------------
        # 3. Generate new entry
        # --------------------------------------------------------------

        return self._generate_entry_signal(working_candle)

    # ------------------------------------------------------------------
    # Tick processing
    # ------------------------------------------------------------------

    def on_tick(
        self,
        price: float,
        timestamp: datetime,
    ) -> Optional[StrategySignal]:
        """
        Tick timestamps are already real event timestamps.
        No timeframe offset is added here.
        """
        if self.position == 0:
            return None

        ts = _normalize_ist_naive(timestamp)
        px = float(price)

        # Mandatory square-off.
        if ts.time() >= self.config.square_off_time:
            return StrategySignal(
                action=SignalAction.EXIT,
                symbol=self.symbol,
                timestamp=ts,
                price=px,
                reason="TIME_SQUARE_OFF",
            )

        # --------------------------------------------------------------
        # LONG
        # --------------------------------------------------------------

        if self.position == 1:
            # Breakeven activation.
            if (
                not self.trailing_breakeven_active
                and self.initial_risk_dist > 0
            ):
                trigger = (
                    self.entry_price
                    + (
                        self.config.breakeven_r_multiple
                        * self.initial_risk_dist
                    )
                )

                if px >= trigger:
                    self.stop_loss = self.entry_price
                    self.trailing_breakeven_active = True

            # Stop / breakeven stop.
            if px <= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.stop_loss,
                    reason=(
                        "BREAKEVEN_SL"
                        if self.trailing_breakeven_active
                        else "STOP_LOSS"
                    ),
                )

            # Target.
            if px >= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

        # --------------------------------------------------------------
        # SHORT
        # --------------------------------------------------------------

        elif self.position == -1:
            # Breakeven activation.
            if (
                not self.trailing_breakeven_active
                and self.initial_risk_dist > 0
            ):
                trigger = (
                    self.entry_price
                    - (
                        self.config.breakeven_r_multiple
                        * self.initial_risk_dist
                    )
                )

                if px <= trigger:
                    self.stop_loss = self.entry_price
                    self.trailing_breakeven_active = True

            # Stop / breakeven stop.
            if px >= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.stop_loss,
                    reason=(
                        "BREAKEVEN_SL"
                        if self.trailing_breakeven_active
                        else "STOP_LOSS"
                    ),
                )

            # Target.
            if px <= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

        return None

    # ------------------------------------------------------------------
    # Candle-based position exits
    # ------------------------------------------------------------------

    def _check_active_position_exits(
        self,
        candle: dict,
    ) -> Optional[StrategySignal]:
        """
        Candle OHLC exit handling.

        Ordering:
        1. Gap through target/stop at candle open.
        2. Same-candle target + stop collision.
        3. Target / stop.
        4. Breakeven activation happens AFTER this candle's exit checks,
           so activation applies from the next candle onward.

        This prevents look-ahead within a single completed bar.
        """
        bar_time = _normalize_ist_naive(candle["datetime"])

        open_price = float(candle["open"])
        high = float(candle["high"])
        low = float(candle["low"])

        # --------------------------------------------------------------
        # LONG
        # --------------------------------------------------------------

        if self.position == 1:
            # Gap-open handling.
            if open_price >= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=open_price,
                    reason="PROFIT_TARGET",
                )

            if open_price <= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=open_price,
                    reason=(
                        "BREAKEVEN_SL"
                        if self.trailing_breakeven_active
                        else "STOP_LOSS"
                    ),
                )

            hit_target = high >= self.target
            hit_stop = low <= self.stop_loss

            # Conservative assumption: stop first when both are touched
            # in an OHLC bar because the exact intrabar path is unknown.
            if hit_target and hit_stop:
                if self.execution_policy == "OPTIMISTIC":
                    return StrategySignal(
                        action=SignalAction.EXIT,
                        symbol=self.symbol,
                        timestamp=bar_time,
                        price=self.target,
                        reason="PROFIT_TARGET",
                    )

                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=self.stop_loss,
                    reason=(
                        "BREAKEVEN_SL"
                        if self.trailing_breakeven_active
                        else "STOP_LOSS"
                    ),
                )

            if hit_stop:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=self.stop_loss,
                    reason=(
                        "BREAKEVEN_SL"
                        if self.trailing_breakeven_active
                        else "STOP_LOSS"
                    ),
                )

            if hit_target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

            # Breakeven activation applies only after this bar finishes.
            if not self.trailing_breakeven_active:
                trigger = (
                    self.entry_price
                    + (
                        self.config.breakeven_r_multiple
                        * self.initial_risk_dist
                    )
                )

                if high >= trigger:
                    self.stop_loss = self.entry_price
                    self.trailing_breakeven_active = True

        # --------------------------------------------------------------
        # SHORT
        # --------------------------------------------------------------

        elif self.position == -1:
            # Gap-open handling.
            if open_price <= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=open_price,
                    reason="PROFIT_TARGET",
                )

            if open_price >= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=open_price,
                    reason=(
                        "BREAKEVEN_SL"
                        if self.trailing_breakeven_active
                        else "STOP_LOSS"
                    ),
                )

            hit_target = low <= self.target
            hit_stop = high >= self.stop_loss

            # Conservative same-candle collision handling.
            if hit_target and hit_stop:
                if self.execution_policy == "OPTIMISTIC":
                    return StrategySignal(
                        action=SignalAction.EXIT,
                        symbol=self.symbol,
                        timestamp=bar_time,
                        price=self.target,
                        reason="PROFIT_TARGET",
                    )

                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=self.stop_loss,
                    reason=(
                        "BREAKEVEN_SL"
                        if self.trailing_breakeven_active
                        else "STOP_LOSS"
                    ),
                )

            if hit_stop:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=self.stop_loss,
                    reason=(
                        "BREAKEVEN_SL"
                        if self.trailing_breakeven_active
                        else "STOP_LOSS"
                    ),
                )

            if hit_target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=bar_time,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

            # Breakeven activation applies only after this bar finishes.
            if not self.trailing_breakeven_active:
                trigger = (
                    self.entry_price
                    - (
                        self.config.breakeven_r_multiple
                        * self.initial_risk_dist
                    )
                )

                if low <= trigger:
                    self.stop_loss = self.entry_price
                    self.trailing_breakeven_active = True

        return None