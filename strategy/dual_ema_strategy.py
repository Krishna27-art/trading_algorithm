"""
Adaptive Volatility-Buffered Dual-EMA Intraday Trend Strategy.

Correctness rules
-----------------
- Input 15-minute timestamps are treated as candle OPEN timestamps by default.
  Signal/exit timestamps are shifted to the candle COMPLETION time.
- EMA9 and EMA21 are calculated continuously across sessions.
- SMA200 is a real 200-bar SMA; no entry is allowed before 200 valid bars.
- ATR14 uses Wilder/RMA smoothing, not a simple rolling mean.
- Entry requires an actual EMA9/EMA21 cross into the ATR buffer and SMA200
  trend confirmation.
- Stop distance is ATR based and must respect the instrument risk cap.
- Target defaults to the configured risk/reward ratio.
- Every signal is validated:
      LONG:  stop < entry < target
      SHORT: target < entry < stop
- One trade per instrument per day is enforced by BaseStrategy state.
- OHLC exits use conservative same-candle handling.
- Gap-through-stop/target exits are filled at the candle OPEN.
- Breakeven activates at the configured R multiple.
"""

from datetime import date, datetime, time
from typing import List, Optional, Tuple

import pandas as pd

from config.settings import (
    InstrumentConfig,
    InstrumentType,
    StrategyConfig,
    settings,
)
from monitoring.logger import logger
from strategy.base_strategy import (
    BaseStrategy,
    SignalAction,
    StrategySignal,
)


REQUIRED_COLUMNS = {"datetime", "high", "low", "close"}


def _normalize_ist_naive(value) -> datetime:
    """
    Normalize timestamp to Asia/Kolkata and return a naive datetime.

    Naive timestamps are assumed to already represent IST.
    """
    ts = pd.Timestamp(value)

    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)

    return ts.to_pydatetime()


def _wilder_atr(
    df: pd.DataFrame,
    period: int = 14,
) -> pd.Series:
    """
    Wilder ATR / RMA-style ATR.

    True Range:
        max(
            high-low,
            abs(high-prev_close),
            abs(low-prev_close)
        )

    Wilder smoothing:
        alpha = 1 / period
    """
    prev_close = df["close"].shift(1)

    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return tr.ewm(
        alpha=1.0 / period,
        adjust=False,
        min_periods=period,
    ).mean()


def _compute_indicators(
    bars: pd.DataFrame,
) -> pd.DataFrame:
    """
    Add EMA9, EMA21, SMA200 and Wilder ATR14.

    The input must be chronological.
    """
    df = bars.copy()

    for col in ("high", "low", "close"):
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce",
        )

    df = df.dropna(
        subset=["high", "low", "close"]
    ).copy()

    df = df[
        df["low"] <= df["high"]
    ].copy()

    df["ema9"] = df["close"].ewm(
        span=9,
        adjust=False,
        min_periods=9,
    ).mean()

    df["ema21"] = df["close"].ewm(
        span=21,
        adjust=False,
        min_periods=21,
    ).mean()

    df["sma200"] = df["close"].rolling(
        window=200,
        min_periods=200,
    ).mean()

    df["atr14"] = _wilder_atr(
        df,
        period=14,
    )

    return df


class BufferedDualEMAStrategy(BaseStrategy):
    """
    15-minute Dual EMA crossover strategy.

    Default entry model:

        LONG
        ----
        previous EMA9 <= previous EMA21
        current EMA9 > current EMA21 + ATR buffer
        close > EMA9
        close > SMA200

        SHORT
        -----
        previous EMA9 >= previous EMA21
        current EMA9 < current EMA21 - ATR buffer
        close < EMA9
        close < SMA200

    Stop:
        1.2 * ATR14 by default

    Target:
        configured risk_reward_ratio * actual stop distance

    This class intentionally does NOT auto-register entries/exits.
    The backtest/execution layer must call:
        register_trade_entry(...)
        register_trade_exit()
    after the order fill/exit is actually accepted.
    """

    def __init__(
        self,
        instrument: InstrumentConfig,
        strategy_config: StrategyConfig = settings.strategy,
        buffer_gamma: float = 0.175,
        stop_atr_multiple: float = 1.2,
        target_atr_multiple: Optional[float] = None,
        session_start: Optional[time] = None,
        session_end: Optional[time] = None,
        min_warmup_bars: int = 200,
        candle_timestamps_are_open: bool = True,
        execution_policy: str = "CONSERVATIVE",
        require_crossover: bool = True,
    ):
        super().__init__(instrument.symbol)

        self.instrument = instrument
        self.config = strategy_config

        if buffer_gamma < 0:
            raise ValueError(
                "buffer_gamma must be >= 0"
            )

        if stop_atr_multiple <= 0:
            raise ValueError(
                "stop_atr_multiple must be > 0"
            )

        if (
            target_atr_multiple is not None
            and target_atr_multiple <= 0
        ):
            raise ValueError(
                "target_atr_multiple must be > 0"
            )

        self.buffer_gamma = float(
            buffer_gamma
        )

        self.stop_atr_multiple = float(
            stop_atr_multiple
        )

        self.target_atr_multiple = (
            float(target_atr_multiple)
            if target_atr_multiple is not None
            else None
        )

        # Use the system-wide entry schedule by default.
        self.session_start = (
            session_start
            if session_start is not None
            else getattr(
                self.config,
                "entry_start",
                time(9, 45),
            )
        )

        self.session_end = (
            session_end
            if session_end is not None
            else getattr(
                self.config,
                "entry_end",
                time(13, 30),
            )
        )

        # A real SMA200 needs 200 valid bars.
        self.min_warmup_bars = max(
            200,
            int(min_warmup_bars),
        )

        self.candle_timestamps_are_open = bool(
            candle_timestamps_are_open
        )

        policy = getattr(
            execution_policy,
            "value",
            execution_policy,
        )

        self.execution_policy = str(
            policy
        ).upper()

        if self.execution_policy not in {
            "CONSERVATIVE",
            "OPTIMISTIC",
        }:
            raise ValueError(
                "execution_policy must be "
                "CONSERVATIVE or OPTIMISTIC"
            )

        self.require_crossover = bool(
            require_crossover
        )

        self.current_date: Optional[date] = None

        self.warm_history = pd.DataFrame(
            columns=[
                "datetime",
                "high",
                "low",
                "close",
            ]
        )

        self.today_bars: List[dict] = []

    # ------------------------------------------------------------------
    # TIMESTAMP / SESSION
    # ------------------------------------------------------------------

    def _event_timestamp(
        self,
        raw_timestamp,
    ) -> datetime:
        """
        Convert candle OPEN timestamp into candle COMPLETION timestamp.

        Example for 15-minute data:

            09:15 -> completed at 09:30
            09:30 -> completed at 09:45
            09:45 -> completed at 10:00
        """
        ts = _normalize_ist_naive(
            raw_timestamp
        )

        if self.candle_timestamps_are_open:
            minutes = int(
                getattr(
                    self.config,
                    "candle_timeframe_minutes",
                    15,
                )
            )

            if minutes <= 0:
                raise ValueError(
                    "candle_timeframe_minutes must be > 0"
                )

            ts += pd.Timedelta(
                minutes=minutes
            ).to_pytimedelta()

        return ts

    def reset_session(
        self,
        session_date: date,
    ):
        """
        Reset only intraday state.

        Rolling indicator history is deliberately preserved.
        """
        self.current_date = session_date

        self.position = 0
        self.entry_price = 0.0
        self.stop_loss = 0.0
        self.target = 0.0
        self.initial_risk_dist = 0.0
        self.trailing_breakeven_active = False
        self.trades_today = 0

        self.today_bars = []

    # ------------------------------------------------------------------
    # CONTEXT / WARMUP
    # ------------------------------------------------------------------

    def seed_context(
        self,
        historical_bars: pd.DataFrame,
    ) -> None:
        """
        Seed cross-day indicator history.

        historical_bars must represent ONLY prior sessions.
        """
        if (
            historical_bars is None
            or historical_bars.empty
        ):
            self.warm_history = pd.DataFrame(
                columns=[
                    "datetime",
                    "high",
                    "low",
                    "close",
                ]
            )
            return

        missing = (
            REQUIRED_COLUMNS
            - set(historical_bars.columns)
        )

        if missing:
            raise ValueError(
                "Dual EMA context missing columns: "
                f"{sorted(missing)}"
            )

        data = historical_bars[
            [
                "datetime",
                "high",
                "low",
                "close",
            ]
        ].copy()

        data["datetime"] = data[
            "datetime"
        ].map(
            _normalize_ist_naive
        )

        for col in (
            "high",
            "low",
            "close",
        ):
            data[col] = pd.to_numeric(
                data[col],
                errors="coerce",
            )

        data = data.dropna(
            subset=[
                "datetime",
                "high",
                "low",
                "close",
            ]
        )

        data = data[
            data["low"] <= data["high"]
        ]

        data = data.sort_values(
            "datetime"
        )

        # Keep bounded history while ensuring enough data
        # for SMA200 plus indicator stabilization.
        max_context = max(
            self.min_warmup_bars * 3,
            600,
        )

        self.warm_history = (
            data
            .tail(max_context)
            .reset_index(drop=True)
        )

    def _combined_bars(
        self,
    ) -> pd.DataFrame:
        """
        Combine historical warmup + today's candles.

        Duplicate timestamps are removed so a candle can never
        accidentally contribute twice to the indicators.
        """
        today = pd.DataFrame(
            self.today_bars
        )

        parts = []

        if not self.warm_history.empty:
            parts.append(
                self.warm_history.copy()
            )

        if not today.empty:
            parts.append(
                today[
                    [
                        "datetime",
                        "high",
                        "low",
                        "close",
                    ]
                ].copy()
            )

        if not parts:
            return pd.DataFrame(
                columns=[
                    "datetime",
                    "high",
                    "low",
                    "close",
                ]
            )

        combined = pd.concat(
            parts,
            ignore_index=True,
        )

        combined["datetime"] = (
            combined["datetime"]
            .map(_normalize_ist_naive)
        )

        combined = combined.sort_values(
            "datetime"
        )

        combined = combined.drop_duplicates(
            subset=["datetime"],
            keep="last",
        )

        return combined.reset_index(
            drop=True
        )

    def _current_indicators(
        self,
        raw_candle: dict,
    ) -> Tuple[
        Optional[pd.Series],
        Optional[pd.Series],
    ]:
        """
        Append ONE candle and calculate indicators.

        Returns:
            current row
            previous row

        The previous row is needed for genuine crossover detection.
        """
        row = {
            "datetime": _normalize_ist_naive(
                raw_candle["datetime"]
            ),
            "high": float(
                raw_candle["high"]
            ),
            "low": float(
                raw_candle["low"]
            ),
            "close": float(
                raw_candle["close"]
            ),
        }

        self.today_bars.append(row)

        combined = self._combined_bars()

        if combined.empty:
            return None, None

        indicators = _compute_indicators(
            combined
        )

        if indicators.empty:
            return None, None

        current = indicators.iloc[-1]

        previous = (
            indicators.iloc[-2]
            if len(indicators) >= 2
            else None
        )

        # A real signal needs all indicator windows to exist.
        if (
            pd.isna(current["ema9"])
            or pd.isna(current["ema21"])
            or pd.isna(current["sma200"])
            or pd.isna(current["atr14"])
        ):
            return None, previous

        return current, previous

    # ------------------------------------------------------------------
    # RISK / PRICE VALIDATION
    # ------------------------------------------------------------------

    def _max_allowed_risk(
        self,
        entry_price: float,
    ) -> float:
        """
        Maximum allowed price distance between entry and initial stop.

        Equities:
            use percentage-based cap when available.

        Futures:
            use configured absolute max_risk_cap.
        """
        entry_price = float(
            entry_price
        )

        if entry_price <= 0:
            return 0.0

        tick = max(
            float(
                getattr(
                    self.instrument,
                    "tick_size",
                    0.05,
                )
            ),
            1e-8,
        )

        if (
            self.instrument.instrument_type
            == InstrumentType.EQUITY
        ):
            risk_pct = float(
                getattr(
                    self.instrument,
                    "equity_orb_max_risk_pct",
                    0.0040,
                )
            )

            return max(
                entry_price * risk_pct,
                tick,
            )

        cap = float(
            getattr(
                self.instrument,
                "max_risk_cap",
                0.0,
            )
        )

        return max(
            cap,
            tick,
        )

    def _tick_round(
        self,
        price: float,
    ) -> float:
        """Round price to instrument tick size."""
        tick = float(
            getattr(
                self.instrument,
                "tick_size",
                0.05,
            )
        )

        if tick <= 0:
            return float(price)

        return round(
            round(
                float(price) / tick
            ) * tick,
            10,
        )

    def _risk_allowed(
        self,
        entry: float,
        stop: float,
    ) -> bool:
        """Reject trades whose actual stop distance exceeds the cap."""
        risk = abs(
            float(entry)
            - float(stop)
        )

        cap = self._max_allowed_risk(
            entry
        )

        return (
            risk > 0
            and risk <= cap
        )

    def _valid_long(
        self,
        entry: float,
        stop: float,
        target: float,
    ) -> bool:
        return (
            stop < entry < target
        )

    def _valid_short(
        self,
        entry: float,
        stop: float,
        target: float,
    ) -> bool:
        return (
            target < entry < stop
        )

    def _target_distance(
        self,
        risk_distance: float,
    ) -> float:
        """
        Target distance.

        Default:
            target = risk * configured R:R

        Compatibility:
            explicit target_atr_multiple preserves a requested
            ATR-based target specification.
        """
        if (
            self.target_atr_multiple
            is not None
        ):
            return risk_distance * (
                self.target_atr_multiple
                / self.stop_atr_multiple
            )

        rr = float(
            getattr(
                self.config,
                "risk_reward_ratio",
                2.0,
            )
        )

        if rr <= 0:
            raise ValueError(
                "risk_reward_ratio must be > 0"
            )

        return (
            risk_distance * rr
        )

    # ------------------------------------------------------------------
    # ENTRY SIGNAL
    # ------------------------------------------------------------------

    def _build_entry_signal(
        self,
        candle: dict,
        current: pd.Series,
        previous: Optional[pd.Series],
    ) -> Optional[StrategySignal]:
        """
        Evaluate current completed candle for an entry.
        """
        max_trades = int(
            getattr(
                self.config,
                "max_trades_per_instrument_day",
                1,
            )
        )

        if (
            self.position != 0
            or self.trades_today >= max_trades
        ):
            return None

        event_timestamp = (
            _normalize_ist_naive(
                candle["datetime"]
            )
        )

        bar_time = event_timestamp.time()

        if not (
            self.session_start
            <= bar_time
            <= self.session_end
        ):
            return None

        close = float(
            candle["close"]
        )

        ema9 = float(
            current["ema9"]
        )

        ema21 = float(
            current["ema21"]
        )

        sma200 = float(
            current["sma200"]
        )

        atr14 = float(
            current["atr14"]
        )

        if atr14 <= 0:
            return None

        buffer = (
            self.buffer_gamma
            * atr14
        )

        cross_long = False
        cross_short = False

        if (
            previous is not None
            and not pd.isna(
                previous["ema9"]
            )
            and not pd.isna(
                previous["ema21"]
            )
        ):
            prev_ema9 = float(
                previous["ema9"]
            )

            prev_ema21 = float(
                previous["ema21"]
            )

            cross_long = (
                prev_ema9
                <= prev_ema21
                and ema9
                > (
                    ema21
                    + buffer
                )
            )

            cross_short = (
                prev_ema9
                >= prev_ema21
                and ema9
                < (
                    ema21
                    - buffer
                )
            )

        if self.require_crossover:
            long_setup = (
                cross_long
                and close > ema9
                and close > sma200
            )

            short_setup = (
                cross_short
                and close < ema9
                and close < sma200
            )
        else:
            # Explicit non-crossover mode for compatibility/testing.
            long_setup = (
                close
                > ema9
                > (
                    ema21 + buffer
                )
                and close > sma200
            )

            short_setup = (
                close
                < ema9
                < (
                    ema21 - buffer
                )
                and close < sma200
            )

        timestamp = event_timestamp

        # --------------------------------------------------------------
        # LONG
        # --------------------------------------------------------------

        if long_setup:
            raw_risk = (
                self.stop_atr_multiple
                * atr14
            )

            stop = self._tick_round(
                close - raw_risk
            )

            actual_risk = (
                close - stop
            )

            target_distance = (
                self._target_distance(
                    actual_risk
                )
            )

            target = self._tick_round(
                close + target_distance
            )

            if not self._risk_allowed(
                close,
                stop,
            ):
                logger.info(
                    f"[{self.symbol}] "
                    f"Dual EMA LONG rejected: "
                    f"risk={actual_risk:.4f} "
                    f"cap="
                    f"{self._max_allowed_risk(close):.4f}"
                )
                return None

            if not self._valid_long(
                close,
                stop,
                target,
            ):
                logger.warning(
                    f"[{self.symbol}] "
                    "Dual EMA LONG rejected: "
                    "invalid price structure."
                )
                return None

            return StrategySignal(
                action=SignalAction.BUY,
                symbol=self.symbol,
                timestamp=timestamp,
                price=close,
                stop_loss=stop,
                target=target,
                reason="DUAL_EMA_CROSS_LONG",
            )

        # --------------------------------------------------------------
        # SHORT
        # --------------------------------------------------------------

        if short_setup:
            raw_risk = (
                self.stop_atr_multiple
                * atr14
            )

            stop = self._tick_round(
                close + raw_risk
            )

            actual_risk = (
                stop - close
            )

            target_distance = (
                self._target_distance(
                    actual_risk
                )
            )

            target = self._tick_round(
                close - target_distance
            )

            if not self._risk_allowed(
                close,
                stop,
            ):
                logger.info(
                    f"[{self.symbol}] "
                    f"Dual EMA SHORT rejected: "
                    f"risk={actual_risk:.4f} "
                    f"cap="
                    f"{self._max_allowed_risk(close):.4f}"
                )
                return None

            if not self._valid_short(
                close,
                stop,
                target,
            ):
                logger.warning(
                    f"[{self.symbol}] "
                    "Dual EMA SHORT rejected: "
                    "invalid price structure."
                )
                return None

            return StrategySignal(
                action=SignalAction.SELL,
                symbol=self.symbol,
                timestamp=timestamp,
                price=close,
                stop_loss=stop,
                target=target,
                reason="DUAL_EMA_CROSS_SHORT",
            )

        return None

    # ------------------------------------------------------------------
    # COMPLETED CANDLE
    # ------------------------------------------------------------------

    def on_candle(
        self,
        candle: dict,
        vwap: float,
    ) -> Optional[StrategySignal]:
        """
        Process one completed candle.

        Incoming timestamps are normally candle-open timestamps.
        """
        try:
            raw_timestamp = (
                _normalize_ist_naive(
                    candle["datetime"]
                )
            )

            event_timestamp = (
                self._event_timestamp(
                    raw_timestamp
                )
            )

            working = {
                "datetime": event_timestamp,
                "open": float(
                    candle["open"]
                ),
                "high": float(
                    candle["high"]
                ),
                "low": float(
                    candle["low"]
                ),
                "close": float(
                    candle["close"]
                ),
                "volume": int(
                    candle.get(
                        "volume",
                        0,
                    )
                ),
            }

        except (
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            logger.warning(
                f"[{self.symbol}] "
                f"Dual EMA invalid candle: "
                f"{exc}"
            )
            return None

        if self.current_date is None:
            self.reset_session(
                event_timestamp.date()
            )
        elif (
            event_timestamp.date()
            != self.current_date
        ):
            self.reset_session(
                event_timestamp.date()
            )

        # --------------------------------------------------------------
        # 1. HARD SQUARE-OFF
        # --------------------------------------------------------------

        if (
            event_timestamp.time()
            >= self.config.square_off_time
            and self.position != 0
        ):
            return StrategySignal(
                action=SignalAction.EXIT,
                symbol=self.symbol,
                timestamp=event_timestamp,
                price=working["close"],
                reason="TIME_SQUARE_OFF",
            )

        # --------------------------------------------------------------
        # 2. EXISTING POSITION
        # --------------------------------------------------------------

        if self.position != 0:
            exit_signal = (
                self._check_active_position_exits(
                    working
                )
            )

            if exit_signal:
                return exit_signal

        # --------------------------------------------------------------
        # 3. INDICATOR UPDATE
        # --------------------------------------------------------------

        current, previous = (
            self._current_indicators(
                {
                    "datetime": raw_timestamp,
                    "high": working["high"],
                    "low": working["low"],
                    "close": working["close"],
                }
            )
        )

        if current is None:
            return None

        # --------------------------------------------------------------
        # 4. ENTRY
        # --------------------------------------------------------------

        return self._build_entry_signal(
            working,
            current,
            previous,
        )

    # ------------------------------------------------------------------
    # LIVE TICK
    # ------------------------------------------------------------------

    def on_tick(
        self,
        price: float,
        timestamp: datetime,
    ) -> Optional[StrategySignal]:
        """
        Monitor live price for:
            square-off
            stop
            target
            breakeven
        """
        if self.position == 0:
            return None

        ts = _normalize_ist_naive(
            timestamp
        )

        px = float(price)

        if (
            ts.time()
            >= self.config.square_off_time
        ):
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
            # Check active stop first.
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

            if px >= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

            self._maybe_activate_breakeven(
                px
            )

        # --------------------------------------------------------------
        # SHORT
        # --------------------------------------------------------------

        elif self.position == -1:
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

            if px <= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

            self._maybe_activate_breakeven(
                px
            )

        return None

    # ------------------------------------------------------------------
    # BREAKEVEN
    # ------------------------------------------------------------------

    def _maybe_activate_breakeven(
        self,
        price: float,
    ) -> None:
        """
        Move initial stop to entry after configured R multiple.
        """
        if (
            self.trailing_breakeven_active
            or self.initial_risk_dist <= 0
        ):
            return

        r_multiple = float(
            getattr(
                self.config,
                "breakeven_r_multiple",
                1.0,
            )
        )

        if r_multiple <= 0:
            return

        if self.position == 1:
            trigger = (
                self.entry_price
                + (
                    r_multiple
                    * self.initial_risk_dist
                )
            )

            if price >= trigger:
                self.stop_loss = (
                    self.entry_price
                )
                self.trailing_breakeven_active = True

        elif self.position == -1:
            trigger = (
                self.entry_price
                - (
                    r_multiple
                    * self.initial_risk_dist
                )
            )

            if price <= trigger:
                self.stop_loss = (
                    self.entry_price
                )
                self.trailing_breakeven_active = True

    # ------------------------------------------------------------------
    # OHLC EXIT ENGINE
    # ------------------------------------------------------------------

    def _check_active_position_exits(
        self,
        candle: dict,
    ) -> Optional[StrategySignal]:
        """
        Realistic completed-bar exit handling.

        Order:
            1. Gap-through-target/stop at OPEN
            2. Same-candle collision
            3. Stop
            4. Target
            5. Breakeven activation

        Breakeven activated by a candle applies to subsequent price action,
        not retroactively to the same candle.
        """
        timestamp = (
            _normalize_ist_naive(
                candle["datetime"]
            )
        )

        open_price = float(
            candle["open"]
        )

        high = float(
            candle["high"]
        )

        low = float(
            candle["low"]
        )

        # ==============================================================
        # LONG
        # ==============================================================

        if self.position == 1:
            # Gap above target.
            if open_price >= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=open_price,
                    reason="PROFIT_TARGET_GAP",
                )

            # Gap below stop.
            if open_price <= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=open_price,
                    reason=(
                        "BREAKEVEN_SL_GAP"
                        if self.trailing_breakeven_active
                        else "STOP_LOSS_GAP"
                    ),
                )

            hit_stop = (
                low <= self.stop_loss
            )

            hit_target = (
                high >= self.target
            )

            # Both stop and target were touched inside one OHLC bar.
            if hit_stop and hit_target:
                if (
                    self.execution_policy
                    == "CONSERVATIVE"
                ):
                    return StrategySignal(
                        action=SignalAction.EXIT,
                        symbol=self.symbol,
                        timestamp=timestamp,
                        price=self.stop_loss,
                        reason=(
                            "BREAKEVEN_SL"
                            if self.trailing_breakeven_active
                            else "STOP_LOSS"
                        ),
                    )

                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

            if hit_stop:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
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
                    timestamp=timestamp,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

            # Activate BE only after existing stop/target checks.
            if not self.trailing_breakeven_active:
                r_multiple = float(
                    getattr(
                        self.config,
                        "breakeven_r_multiple",
                        1.0,
                    )
                )

                if (
                    r_multiple > 0
                    and self.initial_risk_dist > 0
                ):
                    trigger = (
                        self.entry_price
                        + (
                            r_multiple
                            * self.initial_risk_dist
                        )
                    )

                    if high >= trigger:
                        self.stop_loss = (
                            self.entry_price
                        )
                        self.trailing_breakeven_active = True

        # ==============================================================
        # SHORT
        # ==============================================================

        elif self.position == -1:
            # Gap below target.
            if open_price <= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=open_price,
                    reason="PROFIT_TARGET_GAP",
                )

            # Gap above stop.
            if open_price >= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=open_price,
                    reason=(
                        "BREAKEVEN_SL_GAP"
                        if self.trailing_breakeven_active
                        else "STOP_LOSS_GAP"
                    ),
                )

            hit_stop = (
                high >= self.stop_loss
            )

            hit_target = (
                low <= self.target
            )

            # Conservative same-candle collision handling.
            if hit_stop and hit_target:
                if (
                    self.execution_policy
                    == "CONSERVATIVE"
                ):
                    return StrategySignal(
                        action=SignalAction.EXIT,
                        symbol=self.symbol,
                        timestamp=timestamp,
                        price=self.stop_loss,
                        reason=(
                            "BREAKEVEN_SL"
                            if self.trailing_breakeven_active
                            else "STOP_LOSS"
                        ),
                    )

                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

            if hit_stop:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=timestamp,
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
                    timestamp=timestamp,
                    price=self.target,
                    reason="PROFIT_TARGET",
                )

            # Activate BE only after existing stop/target checks.
            if not self.trailing_breakeven_active:
                r_multiple = float(
                    getattr(
                        self.config,
                        "breakeven_r_multiple",
                        1.0,
                    )
                )

                if (
                    r_multiple > 0
                    and self.initial_risk_dist > 0
                ):
                    trigger = (
                        self.entry_price
                        - (
                            r_multiple
                            * self.initial_risk_dist
                        )
                    )

                    if low <= trigger:
                        self.stop_loss = (
                            self.entry_price
                        )
                        self.trailing_breakeven_active = True

        return None