
"""
AOU-OSS Strategy
================

Analytic Ornstein-Uhlenbeck Optimal-Stopping System.

Designed as strategy #7 for the existing trading_algorithm repository.

Core model:
    15-minute OHLCV
        ->
    Rolling 32-bar VWAP anchor
        ->
    Relative price/VWAP spread
        ->
    120-bar OU calibration
        ->
    Kendall/Hurwicz bias correction
        ->
    Half-life gate: 15-60 minutes
        ->
    20-bar Garman-Klass volatility gate
        ->
    Numerical entry-boundary optimization
        ->
    Explicit stop barrier + entry exclusion zone
        ->
    Optional live Level-2 spread gate
        ->
    Mean/equilibrium target
        ->
    Stop / target / time-stop / session square-off

IMPORTANT
---------
This strategy is intentionally self-contained.

It does NOT modify:
    ORB
    CPR
    Dual EMA
    APEX
    Sector Impulse
    SSF Level-5

The live/backtest engine should instantiate this class like:

    AouOssStrategy(instrument, settings.strategy)

Dependencies already used by the repository:
    numpy
    pandas
    scipy

Timestamp convention:
    Input candle timestamps are assumed to be candle OPEN timestamps,
    matching the repository's Zerodha convention.

The strategy evaluates completed candles by shifting the timestamp
forward by the configured candle timeframe.

No entry is generated on incomplete data.
No look-ahead data is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from math import log, pi, sqrt
from typing import Any, Dict, List, Optional, Tuple

from scipy.special import erfcx

import numpy as np
import pandas as pd

from config.settings import (
    InstrumentConfig,
    StrategyConfig,
    settings,
)
from monitoring.logger import logger
from strategy.base_strategy import (
    BaseStrategy,
    SignalAction,
    StrategySignal,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = {
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class OUParameters:
    """
    Calibrated discrete/continuous OU representation.

    Continuous OU:
        dX = kappa * (mu - X) dt + sigma dW

    Discrete representation:
        X_t = alpha + phi * X_(t-1) + epsilon_t
    """

    phi_raw: float
    phi_corrected: float
    alpha: float
    mu: float
    kappa: float
    sigma: float
    stationary_sigma: float
    residual_std: float
    half_life_bars: float
    half_life_minutes: float
    dt_minutes: float


@dataclass
class VolatilityState:
    current_gk: float
    historical_median_gk: float
    ratio: float
    passed: bool


@dataclass
class AOUState:
    """
    Runtime model state exposed for debugging/UI use.
    """

    ready: bool = False

    rolling_vwap: Optional[float] = None
    spread: Optional[float] = None
    equilibrium: Optional[float] = None

    phi: Optional[float] = None
    kappa: Optional[float] = None
    sigma: Optional[float] = None
    stationary_sigma: Optional[float] = None

    half_life_bars: Optional[float] = None
    half_life_minutes: Optional[float] = None

    volatility_ratio: Optional[float] = None
    volatility_passed: bool = False

    entry_boundary_long: Optional[float] = None
    entry_boundary_short: Optional[float] = None

    stop_boundary_long: Optional[float] = None
    stop_boundary_short: Optional[float] = None

    l2_spread_bps: Optional[float] = None
    l2_passed: bool = True

    last_timestamp: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _normalize_ist_naive(value: Any) -> datetime:
    """
    Convert timestamp to IST and remove timezone information.

    Naive timestamps are assumed to already be IST.
    """
    ts = pd.Timestamp(value)

    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)

    return ts.to_pydatetime()


def _completion_timestamp(
    raw_timestamp: Any,
    timeframe_minutes: int,
) -> datetime:
    """
    Zerodha historical 15-minute bars are treated as candle-open timestamps.

    Example:
        10:15 -> candle covers 10:15-10:30
        event timestamp = 10:30
    """
    ts = _normalize_ist_naive(raw_timestamp)
    return ts + pd.Timedelta(minutes=timeframe_minutes).to_pytimedelta()


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------

def _safe_float(value: Any, default: float = np.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default

    if not np.isfinite(result):
        return default

    return result


def _clip(value: float, lo: float, hi: float) -> float:
    return float(min(max(value, lo), hi))


def _tick_round(
    price: float,
    tick_size: float,
    direction: str = "nearest",
) -> float:
    """
    Round price to the instrument tick size.

    direction:
        nearest
        up
        down
    """
    px = float(price)
    tick = max(float(tick_size), 1e-8)

    q = px / tick

    if direction == "up":
        return round(np.ceil(q) * tick, 10)

    if direction == "down":
        return round(np.floor(q) * tick, 10)

    return round(np.round(q) * tick, 10)


# ---------------------------------------------------------------------------
# Rolling VWAP
# ---------------------------------------------------------------------------

def _rolling_volume_weighted_anchor(
    bars: pd.DataFrame,
    window: int,
) -> pd.Series:
    """
    Rolling volume-weighted price anchor.

    Typical price:
        TP = (High + Low + Close) / 3

    RVWAP:
        sum(TP * volume) / sum(volume)

    Unlike cumulative session VWAP, this anchor does not accumulate
    the entire session's historical volume indefinitely.
    """
    if bars.empty:
        return pd.Series(index=bars.index, dtype=float)

    df = bars.copy()

    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)

    typical_price = (high + low + close) / 3.0

    weighted = typical_price * volume

    numerator = weighted.rolling(
        window=window,
        min_periods=window,
    ).sum()

    denominator = volume.rolling(
        window=window,
        min_periods=window,
    ).sum()

    result = numerator / denominator.replace(0.0, np.nan)

    return result


# ---------------------------------------------------------------------------
# Garman-Klass volatility
# ---------------------------------------------------------------------------

def _garman_klass_bar_variance(
    bars: pd.DataFrame,
) -> pd.Series:
    """
    Per-bar Garman-Klass variance estimator.

        0.5 * [ln(H/L)]^2
        - [2 ln(2) - 1] * [ln(C/O)]^2

    Invalid bars are returned as NaN.
    """
    df = bars.copy()

    o = pd.to_numeric(df["open"], errors="coerce")
    h = pd.to_numeric(df["high"], errors="coerce")
    l = pd.to_numeric(df["low"], errors="coerce")
    c = pd.to_numeric(df["close"], errors="coerce")

    valid = (
        (o > 0)
        & (h > 0)
        & (l > 0)
        & (c > 0)
        & (h >= l)
    )

    result = pd.Series(np.nan, index=df.index, dtype=float)

    if not valid.any():
        return result

    ln_hl = np.log(h[valid] / l[valid])
    ln_co = np.log(c[valid] / o[valid])

    variance = (
        0.5 * ln_hl.pow(2)
        - (2.0 * log(2.0) - 1.0) * ln_co.pow(2)
    )

    # Numerical guard.
    result.loc[valid] = variance.clip(lower=0.0)

    return result


def _garman_klass_volatility(
    bars: pd.DataFrame,
    window: int,
) -> float:
    """
    Realized volatility from mean GK variance over the requested window.
    """
    if len(bars) < window:
        return float("nan")

    variances = _garman_klass_bar_variance(
        bars.tail(window)
    ).dropna()

    if len(variances) < window:
        return float("nan")

    return float(np.sqrt(variances.mean()))


# ---------------------------------------------------------------------------
# OU calibration
# ---------------------------------------------------------------------------

def _fit_ou_kendall(
    spread: pd.Series,
    dt_minutes: float,
) -> Optional[OUParameters]:
    """
    Fit an OU process through the equivalent AR(1) representation.

    X_t = alpha + phi * X_(t-1) + epsilon_t

    Kendall/Hurwicz first-order correction:

        phi_corrected ~= phi_hat + (1 + 3*phi_hat) / N

    The correction is clipped into the valid mean-reverting region.

    We require:
        0 < phi < 1

    because:
        phi <= 0     -> oscillatory/non-standard case
        phi >= 1     -> non-mean-reverting process

    Continuous parameters:
        kappa = -ln(phi) / dt
        mu    = alpha / (1 - phi)

    Exact residual-to-OU diffusion conversion:
        Var(eps) = sigma^2 * (1 - exp(-2*kappa*dt)) / (2*kappa)

    therefore:
        sigma = residual_std *
                 sqrt(2*kappa / (1 - exp(-2*kappa*dt)))
    """
    x = pd.Series(spread, dtype=float).dropna()

    if len(x) < 30:
        return None

    y = x.iloc[1:].to_numpy(dtype=float)
    lag = x.iloc[:-1].to_numpy(dtype=float)

    if len(y) != len(lag):
        return None

    if not np.all(np.isfinite(y)) or not np.all(np.isfinite(lag)):
        return None

    if np.std(lag) < 1e-12:
        return None

    # OLS with intercept.
    X = np.column_stack(
        [
            np.ones(len(lag)),
            lag,
        ]
    )

    try:
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return None

    alpha = float(beta[0])
    phi_raw = float(beta[1])

    n = float(len(y))

    # First-order Kendall/Hurwicz correction.
    phi_corrected = phi_raw + (1.0 + 3.0 * phi_raw) / max(n, 1.0)

    # Valid mean-reversion region.
    phi_corrected = _clip(
        phi_corrected,
        1e-6,
        0.999999,
    )

    # Rebuild equilibrium using corrected phi.
    mu = alpha / max(1.0 - phi_corrected, 1e-12)

    dt = float(dt_minutes)

    if dt <= 0:
        return None

    kappa = -log(phi_corrected) / dt

    if not np.isfinite(kappa) or kappa <= 0:
        return None

    residuals = y - (alpha + phi_corrected * lag)

    residual_std = float(np.std(residuals, ddof=1))

    if not np.isfinite(residual_std) or residual_std <= 0:
        return None

    phi_sq = phi_corrected * phi_corrected
    denom = max(1.0 - phi_sq, 1e-12)

    sigma = residual_std * sqrt(
        max(2.0 * kappa / denom, 0.0)
    )

    stationary_sigma = sigma / sqrt(
        max(2.0 * kappa, 1e-12)
    )

    half_life_bars = log(2.0) / (
        -log(phi_corrected)
    )

    half_life_minutes = (
        half_life_bars * dt
    )

    if not all(
        np.isfinite(v)
        for v in [
            phi_raw,
            phi_corrected,
            alpha,
            mu,
            kappa,
            sigma,
            stationary_sigma,
            residual_std,
            half_life_bars,
            half_life_minutes,
        ]
    ):
        return None

    return OUParameters(
        phi_raw=phi_raw,
        phi_corrected=phi_corrected,
        alpha=alpha,
        mu=mu,
        kappa=kappa,
        sigma=sigma,
        stationary_sigma=stationary_sigma,
        residual_std=residual_std,
        half_life_bars=half_life_bars,
        half_life_minutes=half_life_minutes,
        dt_minutes=dt,
    )


# ---------------------------------------------------------------------------
# OU first-passage time approximation
# ---------------------------------------------------------------------------

def _expected_ou_hitting_time_to_mean(
    x: float,
    mu: float,
    kappa: float,
    sigma: float,
) -> float:
    """
    Numerically stable approximation of expected OU first-passage time
    from x toward the equilibrium mean.

    The function is used to build a one-dimensional numerical stopping
    objective. It is intentionally computed numerically instead of using
    a hard-coded closed-form boundary.

    For an OU process:

        dX = kappa(mu-X)dt + sigma dW

    we evaluate the standard integral representation numerically using
    scipy-compatible quadrature implemented through scipy.integrate.quad.

    Returned value is measured in the same time unit as kappa:
        here that is MINUTES.
    """
    if not all(
        np.isfinite(v)
        for v in [x, mu, kappa, sigma]
    ):
        return float("inf")

    if sigma <= 0 or kappa <= 0:
        return float("inf")

    distance = abs(x - mu)

    if distance < 1e-10:
        return 0.0

    # Local import keeps strategy startup light when this strategy
    # is not selected.
    from scipy.integrate import quad

    a = sqrt(kappa) / sigma
    z = a * (x - mu)

    # For a process starting on one side of the mean and targeting the
    # mean, use the corresponding positive distance.
    z_abs = abs(z)

    # Integral form based on the OU scale function.
    #
    # T(z) = sqrt(pi)/kappa * integral_0^z exp(u^2)
    #                                      * erfc(u) du
    #
    # The exact expression has several equivalent representations.
    # Numerical integration is clipped to avoid overflow for very large
    # z values.

    upper = min(z_abs, 8.0)

    if upper <= 1e-8:
        return (
            distance * distance
            / max(sigma * sigma, 1e-12)
        )

    def integrand(u: float) -> float:
        return float(erfcx(u))

    try:
        integral, _ = quad(
            integrand,
            0.0,
            upper,
            limit=100,
        )
    except Exception:
        return float("inf")

    result = (
        sqrt(pi)
        * integral
        / max(kappa, 1e-12)
    )

    if not np.isfinite(result):
        return float("inf")

    return max(result, 0.0)


# ---------------------------------------------------------------------------
# Numerical Bertram-style entry boundary
# ---------------------------------------------------------------------------

def _round_trip_return_rate(
    entry: float,
    mu: float,
    ou: OUParameters,
    transaction_cost_fraction: float,
) -> float:
    """
    Objective used for numerical entry optimization.

    A completed mean-reversion cycle is modeled as:

        entry -> equilibrium

    Approximate net return:

        |entry - mu| - friction

    Expected duration:

        E[tau(entry -> mu)]

    Objective:

        net_return / expected_duration

    This is a numerical optimal-stopping objective rather than a
    hard-coded Taylor approximation.

    `transaction_cost_fraction` is in the same units as the spread
    because the spread itself is represented as a relative price
    deviation.
    """
    raw_reward = abs(entry - mu)

    if raw_reward <= 0:
        return -np.inf

    net_reward = raw_reward - transaction_cost_fraction

    if net_reward <= 0:
        return -np.inf

    expected_time = _expected_ou_hitting_time_to_mean(
        x=entry,
        mu=mu,
        kappa=ou.kappa,
        sigma=ou.sigma,
    )

    if not np.isfinite(expected_time) or expected_time <= 0:
        return -np.inf

    return net_reward / expected_time


def _solve_numerical_entry_boundary(
    ou: OUParameters,
    side: str,
    transaction_cost_fraction: float,
    stop_sigma_multiple: float,
    entry_exclusion_fraction: float,
) -> Optional[float]:
    """
    Solve the one-dimensional entry optimization numerically.

    We search for the maximum of the Bertram-style return-rate objective
    and then refine the optimum around its neighboring grid points using
    scipy.optimize.minimize_scalar.

    The resulting boundary is also constrained to remain inside the
    stop/entry exclusion region.

    side:
        "LONG"
        "SHORT"
    """
    from scipy.optimize import minimize_scalar

    mu = float(ou.mu)
    stationary_sigma = float(ou.stationary_sigma)

    if stationary_sigma <= 0:
        return None

    stop_distance = (
        abs(stop_sigma_multiple)
        * stationary_sigma
    )

    if stop_distance <= 0:
        return None

    if side == "LONG":
        stop = mu - stop_distance

        exclusion_distance = (
            entry_exclusion_fraction
            * stop_distance
        )

        # Entry must be strictly above stop.
        lower = stop + exclusion_distance

        # Cannot enter at/above equilibrium because this is a
        # mean-reversion long setup.
        upper = mu - max(
            transaction_cost_fraction,
            stationary_sigma * 0.02,
        )

        if lower >= upper:
            return None

        grid = np.linspace(
            lower,
            upper,
            80,
        )

        values = np.array(
            [
                _round_trip_return_rate(
                    entry=x,
                    mu=mu,
                    ou=ou,
                    transaction_cost_fraction=transaction_cost_fraction,
                )
                for x in grid
            ],
            dtype=float,
        )

        valid = np.isfinite(values)

        if not np.any(valid):
            return None

        idx = int(np.nanargmax(values))

        lo_idx = max(idx - 1, 0)
        hi_idx = min(idx + 1, len(grid) - 1)

        refine_lo = float(grid[lo_idx])
        refine_hi = float(grid[hi_idx])

        if refine_lo == refine_hi:
            return float(grid[idx])

        result = minimize_scalar(
            lambda x: -_round_trip_return_rate(
                entry=float(x),
                mu=mu,
                ou=ou,
                transaction_cost_fraction=transaction_cost_fraction,
            ),
            bounds=(
                refine_lo,
                refine_hi,
            ),
            method="bounded",
        )

        candidate = float(result.x)

        if not np.isfinite(candidate):
            candidate = float(grid[idx])

        return candidate

    if side == "SHORT":
        stop = mu + stop_distance

        exclusion_distance = (
            entry_exclusion_fraction
            * stop_distance
        )

        upper = stop - exclusion_distance

        lower = mu + max(
            transaction_cost_fraction,
            stationary_sigma * 0.02,
        )

        if lower >= upper:
            return None

        grid = np.linspace(
            lower,
            upper,
            80,
        )

        values = np.array(
            [
                _round_trip_return_rate(
                    entry=x,
                    mu=mu,
                    ou=ou,
                    transaction_cost_fraction=transaction_cost_fraction,
                )
                for x in grid
            ],
            dtype=float,
        )

        valid = np.isfinite(values)

        if not np.any(valid):
            return None

        idx = int(np.nanargmax(values))

        lo_idx = max(idx - 1, 0)
        hi_idx = min(idx + 1, len(grid) - 1)

        refine_lo = float(grid[lo_idx])
        refine_hi = float(grid[hi_idx])

        if refine_lo == refine_hi:
            return float(grid[idx])

        result = minimize_scalar(
            lambda x: -_round_trip_return_rate(
                entry=float(x),
                mu=mu,
                ou=ou,
                transaction_cost_fraction=transaction_cost_fraction,
            ),
            bounds=(
                refine_lo,
                refine_hi,
            ),
            method="bounded",
        )

        candidate = float(result.x)

        if not np.isfinite(candidate):
            candidate = float(grid[idx])

        return candidate

    raise ValueError(
        "side must be LONG or SHORT"
    )


# ---------------------------------------------------------------------------
# Main strategy
# ---------------------------------------------------------------------------

class AouOssStrategy(BaseStrategy):
    """
    AOU-OSS intraday mean-reversion strategy.

    Default statistical configuration:

        timeframe                15 minutes
        RVWAP window              32 bars
        OU calibration            120 bars
        half-life gate            15-60 minutes
        GK volatility window      20 bars
        GK historical median      120 bars
        execution window          10:15-14:15 IST
        square-off                15:00 IST
        maximum holding           4 bars by default
        L2 spread gate             2.5 bps
        transaction friction       7.5 bps round-trip

    NOTE:
    The strategy uses relative spread:

        X_t = close_t / rolling_vwap_t - 1

    This keeps the statistical process comparable across different
    stock prices.
    """

    def __init__(
        self,
        instrument: InstrumentConfig,
        strategy_config: StrategyConfig = settings.strategy,
        *,
        rvwap_window: int = 32,
        calibration_window: int = 120,
        volatility_window: int = 20,
        volatility_history_window: int = 120,
        min_half_life_minutes: float = 15.0,
        max_half_life_minutes: float = 60.0,
        volatility_ratio_max: float = 1.50,
        transaction_friction_bps: float = 7.5,
        l2_spread_max_bps: float = 2.5,
        stop_sigma_multiple: float = 2.0,
        entry_exclusion_fraction: float = 0.25,
        max_hold_bars: Optional[int] = None,
        execution_start: time = time(10, 15),
        execution_end: time = time(14, 15),
        square_off_time: time = time(15, 0),
        candle_timestamps_are_open: bool = True,
    ):
        super().__init__(instrument.symbol)

        self.instrument = instrument
        self.config = strategy_config

        # Statistical configuration.
        self.rvwap_window = int(rvwap_window)
        self.calibration_window = int(calibration_window)
        self.volatility_window = int(volatility_window)
        self.volatility_history_window = int(
            volatility_history_window
        )

        self.min_half_life_minutes = float(
            min_half_life_minutes
        )
        self.max_half_life_minutes = float(
            max_half_life_minutes
        )

        self.volatility_ratio_max = float(
            volatility_ratio_max
        )

        self.transaction_friction_bps = float(
            transaction_friction_bps
        )

        self.transaction_friction = (
            self.transaction_friction_bps / 10_000.0
        )

        self.l2_spread_max_bps = float(
            l2_spread_max_bps
        )

        self.stop_sigma_multiple = float(
            stop_sigma_multiple
        )

        self.entry_exclusion_fraction = float(
            entry_exclusion_fraction
        )

        if max_hold_bars is None:
            self.max_hold_bars = 4
        else:
            self.max_hold_bars = int(max_hold_bars)

        if self.rvwap_window <= 0:
            raise ValueError(
                "rvwap_window must be > 0"
            )

        if self.calibration_window < 30:
            raise ValueError(
                "calibration_window must be >= 30"
            )

        if self.volatility_window <= 0:
            raise ValueError(
                "volatility_window must be > 0"
            )

        if self.volatility_history_window < (
            self.volatility_window
        ):
            raise ValueError(
                "volatility_history_window must be >= "
                "volatility_window"
            )

        if (
            self.min_half_life_minutes <= 0
            or self.max_half_life_minutes
            <= self.min_half_life_minutes
        ):
            raise ValueError(
                "invalid half-life bounds"
            )

        if self.transaction_friction_bps < 0:
            raise ValueError(
                "transaction_friction_bps "
                "cannot be negative"
            )

        if self.l2_spread_max_bps <= 0:
            raise ValueError(
                "l2_spread_max_bps must be > 0"
            )

        if self.stop_sigma_multiple <= 0:
            raise ValueError(
                "stop_sigma_multiple must be > 0"
            )

        if not 0.0 <= self.entry_exclusion_fraction < 1.0:
            raise ValueError(
                "entry_exclusion_fraction must be "
                "in [0, 1)"
            )

        self.execution_start = execution_start
        self.execution_end = execution_end
        self.square_off_time = square_off_time

        self.candle_timestamps_are_open = bool(
            candle_timestamps_are_open
        )

        # Cross-session context.
        self.context_bars = pd.DataFrame(
            columns=[
                "datetime",
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]
        )

        # Current session.
        self.current_date: Optional[date] = None
        self.history_today: List[dict] = []

        # Current model.
        self.ou: Optional[OUParameters] = None
        self.volatility: Optional[VolatilityState] = None

        self.state = AOUState()

        # Optional live Level-2 market context.
        self.market_context: Dict[str, Any] = {}

        # Entry-time bookkeeping.
        self.entry_event_timestamp: Optional[
            datetime
        ] = None

        self.entry_bar_number: Optional[int] = None

        logger.info(
            "[%s] AOU-OSS initialized: "
            "RVWAP=%d, OU=%d, HL=%g-%g min, "
            "GK=%d/%d, friction=%.2f bps, L2<=%.2f bps.",
            self.symbol,
            self.rvwap_window,
            self.calibration_window,
            self.min_half_life_minutes,
            self.max_half_life_minutes,
            self.volatility_window,
            self.volatility_history_window,
            self.transaction_friction_bps,
            self.l2_spread_max_bps,
        )

    # ------------------------------------------------------------------
    # Public market-context hook
    # ------------------------------------------------------------------

    def set_market_context(
        self,
        *,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        best_bid: Optional[float] = None,
        best_ask: Optional[float] = None,
        book_snapshot: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> None:
        """
        Optional live Level-2 context.

        The strategy only needs the best bid/ask to enforce the
        inside-spread gate.

        Accepted aliases:
            bid / best_bid
            ask / best_ask

        An L2 book snapshot can also be supplied.
        """
        context: Dict[str, Any] = dict(kwargs)

        context["bid"] = (
            best_bid if best_bid is not None else bid
        )

        context["ask"] = (
            best_ask if best_ask is not None else ask
        )

        if book_snapshot is not None:
            context["book_snapshot"] = book_snapshot

        self.market_context = context

        spread_bps = self._calculate_l2_spread_bps()

        if spread_bps is None:
            self.state.l2_spread_bps = None
            self.state.l2_passed = False
        else:
            self.state.l2_spread_bps = spread_bps
            self.state.l2_passed = (
                spread_bps <= self.l2_spread_max_bps
            )

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def reset_session(
        self,
        session_date: date,
    ):
        """
        Reset ONLY session state.

        Cross-day statistical context is preserved in
        self.context_bars.
        """
        self.current_date = session_date
        self.history_today.clear()

        # Base position state.
        self.position = 0
        self.entry_price = 0.0
        self.stop_loss = 0.0
        self.target = 0.0
        self.initial_risk_dist = 0.0
        self.trailing_breakeven_active = False
        self.trades_today = 0

        # Model state.
        self.ou = None
        self.volatility = None

        self.entry_event_timestamp = None
        self.entry_bar_number = None

        self.state = AOUState(
            l2_spread_bps=self.state.l2_spread_bps,
            l2_passed=self.state.l2_passed,
        )

        logger.info(
            "[%s] AOU-OSS session reset: %s",
            self.symbol,
            session_date,
        )

    def seed_context(
        self,
        historical_bars: pd.DataFrame,
    ) -> None:
        """
        Seed cross-day historical context.

        The backtester calls this before reset_session().

        We do not generate trades from this data.
        It is only used for rolling statistical warm-up.
        """
        if historical_bars is None:
            self.context_bars = pd.DataFrame()
            return

        if historical_bars.empty:
            self.context_bars = historical_bars.copy()
            return

        df = historical_bars.copy()

        if (
            "datetime" not in df.columns
            and isinstance(df.index, pd.DatetimeIndex)
        ):
            df["datetime"] = df.index

        missing = {
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume",
        } - set(df.columns)

        if missing:
            logger.warning(
                "[%s] AOU-OSS context missing columns: %s",
                self.symbol,
                sorted(missing),
            )
            return

        df["datetime"] = pd.to_datetime(
            df["datetime"]
        )

        for col in [
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]:
            df[col] = pd.to_numeric(
                df[col],
                errors="coerce",
            )

        df = df.dropna(
            subset=[
                "datetime",
                "open",
                "high",
                "low",
                "close",
            ]
        )

        df = df.sort_values(
            "datetime"
        ).reset_index(drop=True)

        self.context_bars = df.tail(
            max(
                self.calibration_window * 3,
                self.volatility_history_window * 3,
                500,
            )
        ).copy()

    # ------------------------------------------------------------------
    # Trade registration
    # ------------------------------------------------------------------

    def register_trade_entry(
        self,
        entry_price: float,
        position: int,
        stop_loss: float,
        target: float,
        risk_dist: float,
    ):
        super().register_trade_entry(
            entry_price=entry_price,
            position=position,
            stop_loss=stop_loss,
            target=target,
            risk_dist=risk_dist,
        )

        self.entry_event_timestamp = (
            self.state.last_timestamp
        )

        self.entry_bar_number = len(
            self.history_today
        )

    def register_trade_exit(self):
        super().register_trade_exit()

        self.entry_event_timestamp = None
        self.entry_bar_number = None

    # ------------------------------------------------------------------
    # Data construction
    # ------------------------------------------------------------------

    def _combined_bars(self) -> pd.DataFrame:
        """
        Combine seeded historical context with today's bars.

        Deduplicates timestamps and sorts chronologically.
        """
        frames = []

        if (
            self.context_bars is not None
            and not self.context_bars.empty
        ):
            frames.append(
                self.context_bars.copy()
            )

        if self.history_today:
            frames.append(
                pd.DataFrame(
                    self.history_today
                )
            )

        if not frames:
            return pd.DataFrame(
                columns=[
                    "datetime",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                ]
            )

        df = pd.concat(
            frames,
            ignore_index=True,
        )

        df["datetime"] = pd.to_datetime(
            df["datetime"]
        )

        for col in [
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]:
            if col in df.columns:
                df[col] = pd.to_numeric(
                    df[col],
                    errors="coerce",
                )

        df = df.drop_duplicates(
            subset=["datetime"],
            keep="last",
        )

        df = df.sort_values(
            "datetime"
        ).reset_index(drop=True)

        return df

    def _calculate_rvwap_and_spread(
        self,
        bars: pd.DataFrame,
    ) -> Tuple[
        Optional[pd.Series],
        Optional[pd.Series],
    ]:
        """
        Calculate rolling VWAP and relative spread.

            RVWAP = rolling volume-weighted typical price

            spread = close / RVWAP - 1
        """
        if len(bars) < self.rvwap_window:
            return None, None

        rvwap = _rolling_volume_weighted_anchor(
            bars,
            window=self.rvwap_window,
        )

        safe_rvwap = rvwap.replace(
            0.0,
            np.nan,
        )

        spread = (
            bars["close"]
            .astype(float)
            / safe_rvwap
            - 1.0
        )

        return rvwap, spread

    # ------------------------------------------------------------------
    # Volatility
    # ------------------------------------------------------------------

    def _calculate_volatility_state(
        self,
        bars: pd.DataFrame,
    ) -> Optional[VolatilityState]:
        """
        Compare current 20-bar Garman-Klass volatility against
        the historical median over the preceding history window.

        This implementation is mathematically equivalent to the previous
        rolling calculation but avoids rebuilding every 20-bar DataFrame
        window in a Python loop.
        """
        required = (
            self.volatility_history_window
            + self.volatility_window
        )

        if len(bars) < required:
            return None

        working = bars.tail(required)

        # --------------------------------------------------------------
        # Calculate each candle's GK variance exactly once.
        # --------------------------------------------------------------
        bar_variance = _garman_klass_bar_variance(
            working
        )

        if bar_variance.empty:
            return None

        # --------------------------------------------------------------
        # Calculate every rolling GK volatility window vectorially.
        #
        # GK volatility = sqrt(mean(GK variance over N bars))
        # --------------------------------------------------------------
        rolling_mean_variance = (
            bar_variance
            .rolling(
                window=self.volatility_window,
                min_periods=self.volatility_window,
            )
            .mean()
        )

        rolling_gk = np.sqrt(
            rolling_mean_variance.clip(lower=0.0)
        )

        current_gk = rolling_gk.iloc[-1]

        if not np.isfinite(current_gk):
            return None

        # --------------------------------------------------------------
        # The original implementation excluded the latest
        # volatility_window bars from the historical reference.
        #
        # Since `working` contains:
        #     history_window + volatility_window
        # bars, removing the final volatility_window rolling values
        # preserves that exact definition.
        # --------------------------------------------------------------
        historical_values = (
            rolling_gk.iloc[
                : -self.volatility_window
            ]
            .dropna()
        )

        if len(historical_values) < 20:
            return None

        historical_median = float(
            np.median(
                historical_values.to_numpy(
                    dtype=float
                )
            )
        )

        if (
            not np.isfinite(
                historical_median
            )
            or historical_median <= 0
        ):
            return None

        ratio = (
            float(current_gk)
            / historical_median
        )

        passed = (
            np.isfinite(ratio)
            and ratio <= self.volatility_ratio_max
        )

        return VolatilityState(
            current_gk=float(current_gk),
            historical_median_gk=historical_median,
            ratio=float(ratio),
            passed=bool(passed),
        )

    # ------------------------------------------------------------------
    # Level-2 spread
    # ------------------------------------------------------------------

    def _calculate_l2_spread_bps(
        self,
    ) -> Optional[float]:
        """
        Calculate inside bid/ask spread in basis points.

            spread_bps =
                (ask-bid) / ((ask+bid)/2) * 10,000
        """
        if not self.market_context:
            return None

        bid = self.market_context.get(
            "best_bid",
            self.market_context.get(
                "bid"
            ),
        )

        ask = self.market_context.get(
            "best_ask",
            self.market_context.get(
                "ask"
            ),
        )

        bid = _safe_float(bid)
        ask = _safe_float(ask)

        if not np.isfinite(bid) or not np.isfinite(ask):
            return None

        if bid <= 0 or ask <= 0 or ask < bid:
            return None

        mid = 0.5 * (bid + ask)

        if mid <= 0:
            return None

        return (
            (ask - bid)
            / mid
            * 10_000.0
        )

    def _passes_l2_gate(self) -> bool:
        """
        If no L2 data is available during historical backtesting,
        the gate is considered unavailable rather than fabricated.

        In live mode, when bid/ask exists, the 2.5 bps limit is enforced.
        """
        spread_bps = (
            self._calculate_l2_spread_bps()
        )

        if spread_bps is None:
            self.state.l2_spread_bps = None
            self.state.l2_passed = False
            return False

        passed = (
            spread_bps
            <= self.l2_spread_max_bps
        )

        self.state.l2_spread_bps = spread_bps
        self.state.l2_passed = passed

        return passed

    # ------------------------------------------------------------------
    # Statistical model update
    # ------------------------------------------------------------------

    def _update_model(
        self,
    ) -> bool:
        """
        Recalculate the AOU-OSS statistical state using all data available
        UP TO the current completed candle.

        There is no look-ahead.
        """
        bars = self._combined_bars()

        if len(bars) < max(
            self.calibration_window
            + self.rvwap_window,
            self.volatility_history_window
            + self.volatility_window,
        ):
            self.state.ready = False
            return False

        rvwap, spread = (
            self._calculate_rvwap_and_spread(
                bars
            )
        )

        if rvwap is None or spread is None:
            self.state.ready = False
            return False

        valid_spread = spread.dropna()

        if len(valid_spread) < (
            self.calibration_window
        ):
            self.state.ready = False
            return False

        calibration = valid_spread.tail(
            self.calibration_window
        )

        dt_minutes = float(
            self.config.candle_timeframe_minutes
        )

        ou = _fit_ou_kendall(
            calibration,
            dt_minutes=dt_minutes,
        )

        if ou is None:
            self.state.ready = False
            return False

        half_life_ok = (
            ou.half_life_minutes
            >= self.min_half_life_minutes
            and ou.half_life_minutes
            <= self.max_half_life_minutes
        )

        volatility = (
            self._calculate_volatility_state(
                bars
            )
        )

        if volatility is None:
            self.state.ready = False
            return False

        if not half_life_ok:
            self.state.ready = False
            self.ou = ou
            self.volatility = volatility

            latest_rvwap = rvwap.iloc[-1]
            self.state.rolling_vwap = (
                float(latest_rvwap)
                if np.isfinite(latest_rvwap)
                else None
            )

            latest_spread = spread.iloc[-1]
            self.state.spread = (
                float(latest_spread)
                if np.isfinite(latest_spread)
                else None
            )

            self.state.equilibrium = (
                float(ou.mu)
            )

            self.state.phi = (
                float(ou.phi_corrected)
            )
            self.state.kappa = (
                float(ou.kappa)
            )
            self.state.sigma = (
                float(ou.sigma)
            )
            self.state.stationary_sigma = (
                float(ou.stationary_sigma)
            )

            self.state.half_life_bars = (
                float(ou.half_life_bars)
            )
            self.state.half_life_minutes = (
                float(ou.half_life_minutes)
            )

            self.state.volatility_ratio = (
                float(volatility.ratio)
            )
            self.state.volatility_passed = (
                bool(volatility.passed)
            )

            return False

        self.ou = ou
        self.volatility = volatility

        latest_rvwap = rvwap.iloc[-1]
        latest_spread = spread.iloc[-1]

        if not np.isfinite(latest_rvwap):
            return False

        if not np.isfinite(latest_spread):
            return False

        # --------------------------------------------------------------
        # Numerical optimal-stopping boundary.
        #
        # The LONG and SHORT problems are symmetric around OU equilibrium:
        #
        #     LONG  = mu - distance
        #     SHORT = mu + distance
        #
        # The objective depends only on |entry - mu|, so solving the
        # symmetric problem twice is unnecessary.
        # --------------------------------------------------------------
        long_entry = _solve_numerical_entry_boundary(
            ou=ou,
            side="LONG",
            transaction_cost_fraction=(
                self.transaction_friction
            ),
            stop_sigma_multiple=(
                self.stop_sigma_multiple
            ),
            entry_exclusion_fraction=(
                self.entry_exclusion_fraction
            ),
        )

        short_entry = None

        if long_entry is not None:
            short_entry = (
                2.0 * float(ou.mu)
                - float(long_entry)
            )

            if not np.isfinite(short_entry):
                short_entry = None

        if (
            long_entry is None
            or short_entry is None
        ):
            self.state.ready = False
            return False

        # Explicit exogenous stop barriers.
        stop_distance = (
            self.stop_sigma_multiple
            * ou.stationary_sigma
        )

        stop_long = (
            ou.mu
            - stop_distance
        )

        stop_short = (
            ou.mu
            + stop_distance
        )

        # Publish state.
        self.state.ready = (
            volatility.passed
        )

        self.state.rolling_vwap = (
            float(latest_rvwap)
        )

        self.state.spread = (
            float(latest_spread)
        )

        self.state.equilibrium = (
            float(ou.mu)
        )

        self.state.phi = (
            float(ou.phi_corrected)
        )

        self.state.kappa = (
            float(ou.kappa)
        )

        self.state.sigma = (
            float(ou.sigma)
        )

        self.state.stationary_sigma = (
            float(ou.stationary_sigma)
        )

        self.state.half_life_bars = (
            float(ou.half_life_bars)
        )

        self.state.half_life_minutes = (
            float(ou.half_life_minutes)
        )

        self.state.volatility_ratio = (
            float(volatility.ratio)
        )

        self.state.volatility_passed = (
            bool(volatility.passed)
        )

        self.state.entry_boundary_long = (
            float(long_entry)
        )

        self.state.entry_boundary_short = (
            float(short_entry)
        )

        self.state.stop_boundary_long = (
            float(stop_long)
        )

        self.state.stop_boundary_short = (
            float(stop_short)
        )

        return bool(
            volatility.passed
        )

    # ------------------------------------------------------------------
    # Signal validation
    # ------------------------------------------------------------------

    def _valid_long_levels(
        self,
        entry_price: float,
        stop_price: float,
        target_price: float,
    ) -> bool:
        return (
            np.isfinite(entry_price)
            and np.isfinite(stop_price)
            and np.isfinite(target_price)
            and stop_price < entry_price
            and entry_price < target_price
        )

    def _valid_short_levels(
        self,
        entry_price: float,
        stop_price: float,
        target_price: float,
    ) -> bool:
        return (
            np.isfinite(entry_price)
            and np.isfinite(stop_price)
            and np.isfinite(target_price)
            and target_price < entry_price
            and entry_price < stop_price
        )

    # ------------------------------------------------------------------
    # Entry generation
    # ------------------------------------------------------------------

    def _entry_signal(
        self,
        candle: dict,
    ) -> Optional[StrategySignal]:
        """
        Generate a new entry only when all AOU-OSS gates are satisfied.

        LONG:
            spread <= optimal long-entry boundary
            spread > stop barrier
            spread > lower exclusion boundary
            volatility regime passes
            L2 spread passes

        SHORT:
            spread >= optimal short-entry boundary
            spread < stop barrier
            spread < upper exclusion boundary
            volatility regime passes
            L2 spread passes
        """
        if self.position != 0:
            return None

        if self.trades_today >= (
            self.config.max_trades_per_instrument_day
        ):
            return None

        ts = _normalize_ist_naive(
            candle["datetime"]
        )

        if not (
            self.execution_start
            <= ts.time()
            <= self.execution_end
        ):
            return None

        if not self.state.ready:
            return None

        if self.ou is None:
            return None

        if self.volatility is None:
            return None

        if not self.volatility.passed:
            return None

        if not self._passes_l2_gate():
            return None

        current_close = float(
            candle["close"]
        )

        if current_close <= 0:
            return None

        rvwap = self.state.rolling_vwap

        if rvwap is None or rvwap <= 0:
            return None

        current_spread = (
            current_close / rvwap
            - 1.0
        )

        mu = float(
            self.ou.mu
        )

        long_entry_spread = (
            self.state.entry_boundary_long
        )

        short_entry_spread = (
            self.state.entry_boundary_short
        )

        stop_long_spread = (
            self.state.stop_boundary_long
        )

        stop_short_spread = (
            self.state.stop_boundary_short
        )

        if (
            long_entry_spread is None
            or short_entry_spread is None
            or stop_long_spread is None
            or stop_short_spread is None
        ):
            return None

        # --------------------------------------------------------------
        # LONG
        # --------------------------------------------------------------

        if (
            current_spread
            <= long_entry_spread
            and current_spread
            > stop_long_spread
            and current_spread
            < mu
        ):
            # Price-space stop barrier.
            stop_price = (
                rvwap
                * (1.0 + stop_long_spread)
            )

            # Target is current rolling equilibrium.
            target_price = (
                rvwap
                * (1.0 + mu)
            )

            entry_price = current_close

            stop_price = _tick_round(
                stop_price,
                self.instrument.tick_size,
                direction="down",
            )

            target_price = _tick_round(
                target_price,
                self.instrument.tick_size,
                direction="up",
            )

            if not self._valid_long_levels(
                entry_price,
                stop_price,
                target_price,
            ):
                return None

            reason = (
                "AOU_OSS_LONG:"
                f"spread={current_spread:.6f},"
                f"entry_boundary={long_entry_spread:.6f},"
                f"mu={mu:.6f},"
                f"half_life="
                f"{self.ou.half_life_minutes:.2f}m,"
                f"vol_ratio="
                f"{self.volatility.ratio:.3f}"
            )

            return StrategySignal(
                action=SignalAction.BUY,
                symbol=self.symbol,
                timestamp=ts,
                price=float(entry_price),
                stop_loss=float(stop_price),
                target=float(target_price),
                reason=reason,
                order_type="LIMIT",
                product="MIS",
            )

        # --------------------------------------------------------------
        # SHORT
        # --------------------------------------------------------------

        if (
            current_spread
            >= short_entry_spread
            and current_spread
            < stop_short_spread
            and current_spread
            > mu
        ):
            stop_price = (
                rvwap
                * (1.0 + stop_short_spread)
            )

            target_price = (
                rvwap
                * (1.0 + mu)
            )

            entry_price = current_close

            stop_price = _tick_round(
                stop_price,
                self.instrument.tick_size,
                direction="up",
            )

            target_price = _tick_round(
                target_price,
                self.instrument.tick_size,
                direction="down",
            )

            if not self._valid_short_levels(
                entry_price,
                stop_price,
                target_price,
            ):
                return None

            reason = (
                "AOU_OSS_SHORT:"
                f"spread={current_spread:.6f},"
                f"entry_boundary={short_entry_spread:.6f},"
                f"mu={mu:.6f},"
                f"half_life="
                f"{self.ou.half_life_minutes:.2f}m,"
                f"vol_ratio="
                f"{self.volatility.ratio:.3f}"
            )

            return StrategySignal(
                action=SignalAction.SELL,
                symbol=self.symbol,
                timestamp=ts,
                price=float(entry_price),
                stop_loss=float(stop_price),
                target=float(target_price),
                reason=reason,
                order_type="LIMIT",
                product="MIS",
            )

        return None

    # ------------------------------------------------------------------
    # Time-stop
    # ------------------------------------------------------------------

    def _time_stop_due(
        self,
        current_timestamp: datetime,
    ) -> bool:
        """
        Time stop based on bars held.

        The entry timestamp itself is a completed-bar event timestamp.
        """
        if self.position == 0:
            return False

        if (
            self.entry_event_timestamp is None
        ):
            return False

        delta = (
            current_timestamp
            - self.entry_event_timestamp
        )

        if delta.total_seconds() <= 0:
            return False

        bars_held = (
            delta.total_seconds()
            / (
                self.config.candle_timeframe_minutes
                * 60.0
            )
        )

        return (
            bars_held
            >= self.max_hold_bars
        )

    # ------------------------------------------------------------------
    # Candle exits
    # ------------------------------------------------------------------

    def _check_active_position_exits(
        self,
        candle: dict,
    ) -> Optional[StrategySignal]:
        """
        Conservative OHLC exit handling.

        For same-bar target + stop collisions,
        stop is assumed to occur first.

        Gap-through:
            exit at candle open.
        """
        if self.position == 0:
            return None

        ts = _normalize_ist_naive(
            candle["datetime"]
        )

        open_p = float(
            candle.get(
                "open",
                candle["close"],
            )
        )

        high = float(
            candle["high"]
        )

        low = float(
            candle["low"]
        )

        # --------------------------------------------------------------
        # Global square-off
        # --------------------------------------------------------------

        if ts.time() >= self.square_off_time:
            return StrategySignal(
                action=SignalAction.EXIT,
                symbol=self.symbol,
                timestamp=ts,
                price=float(
                    open_p
                    if np.isfinite(open_p)
                    else candle["close"]
                ),
                reason="AOU_OSS_TIME_SQUARE_OFF",
            )

        # --------------------------------------------------------------
        # Statistical duration stop
        # --------------------------------------------------------------

        if self._time_stop_due(ts):
            return StrategySignal(
                action=SignalAction.EXIT,
                symbol=self.symbol,
                timestamp=ts,
                price=float(
                    candle["close"]
                ),
                reason="AOU_OSS_TIME_STOP",
            )

        # --------------------------------------------------------------
        # LONG
        # --------------------------------------------------------------

        if self.position == 1:

            # Gap through target.
            if open_p >= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=open_p,
                    reason="AOU_OSS_PROFIT_TARGET",
                )

            # Gap through stop.
            if open_p <= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=open_p,
                    reason="AOU_OSS_STOP_LOSS",
                )

            hit_target = (
                high >= self.target
            )

            hit_stop = (
                low <= self.stop_loss
            )

            # Conservative:
            # if both occur inside the same bar,
            # assume stop first.
            if hit_stop:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.stop_loss,
                    reason="AOU_OSS_STOP_LOSS",
                )

            if hit_target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.target,
                    reason="AOU_OSS_PROFIT_TARGET",
                )

        # --------------------------------------------------------------
        # SHORT
        # --------------------------------------------------------------

        elif self.position == -1:

            # Gap through target.
            if open_p <= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=open_p,
                    reason="AOU_OSS_PROFIT_TARGET",
                )

            # Gap through stop.
            if open_p >= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=open_p,
                    reason="AOU_OSS_STOP_LOSS",
                )

            hit_target = (
                low <= self.target
            )

            hit_stop = (
                high >= self.stop_loss
            )

            if hit_stop:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.stop_loss,
                    reason="AOU_OSS_STOP_LOSS",
                )

            if hit_target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.target,
                    reason="AOU_OSS_PROFIT_TARGET",
                )

        return None

    # ------------------------------------------------------------------
    # Main candle interface
    # ------------------------------------------------------------------

    def on_candle(
        self,
        candle: dict,
        vwap: float,
    ) -> Optional[StrategySignal]:
        """
        Process ONE COMPLETED 15-minute candle.

        The repository backtester passes a session VWAP value, but
        AOU-OSS deliberately ignores that value and calculates its own
        rolling VWAP anchor because its specification requires a rolling
        volume-weighted anchor rather than cumulative session VWAP.
        """
        if candle is None:
            return None

        raw_timestamp = _normalize_ist_naive(
            candle["datetime"]
        )

        if self.candle_timestamps_are_open:
            event_timestamp = (
                _completion_timestamp(
                    raw_timestamp,
                    self.config.candle_timeframe_minutes,
                )
            )
        else:
            event_timestamp = raw_timestamp

        working_candle = {
            "datetime": event_timestamp,
            "open": _safe_float(
                candle.get("open")
            ),
            "high": _safe_float(
                candle.get("high")
            ),
            "low": _safe_float(
                candle.get("low")
            ),
            "close": _safe_float(
                candle.get("close")
            ),
            "volume": _safe_float(
                candle.get("volume", 0),
                default=0.0,
            ),
        }

        # Basic data validation.
        if not all(
            np.isfinite(
                working_candle[col]
            )
            for col in [
                "open",
                "high",
                "low",
                "close",
            ]
        ):
            return None

        if (
            working_candle["low"]
            > working_candle["high"]
        ):
            return None

        # Session initialization.
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

        # Store candle using ORIGINAL candle-open timestamp.
        history_candle = dict(
            working_candle
        )
        history_candle["datetime"] = (
            raw_timestamp
        )

        self.history_today.append(
            history_candle
        )

        self.state.last_timestamp = (
            event_timestamp
        )

        # --------------------------------------------------------------
        # Active-position management comes BEFORE new entries.
        # --------------------------------------------------------------

        if self.position != 0:
            exit_signal = (
                self._check_active_position_exits(
                    working_candle
                )
            )

            if exit_signal is not None:
                return exit_signal

            # No additional entry while holding.
            return None

        # --------------------------------------------------------------
        # Do not open new trades after square-off.
        # --------------------------------------------------------------

        if (
            event_timestamp.time()
            >= self.square_off_time
        ):
            return None

        # --------------------------------------------------------------
        # Update all statistical calculations using data available
        # through this completed candle only.
        # --------------------------------------------------------------

        model_ready = self._update_model()

        if not model_ready:
            return None

        # --------------------------------------------------------------
        # Generate entry.
        # --------------------------------------------------------------

        return self._entry_signal(
            working_candle
        )

    # ------------------------------------------------------------------
    # Tick interface
    # ------------------------------------------------------------------

    def on_tick(
        self,
        price: float,
        timestamp: datetime,
    ) -> Optional[StrategySignal]:
        """
        Live monitoring of stop / target / time-stop / square-off.

        Tick timestamps are already event timestamps.
        """
        if self.position == 0:
            return None

        px = float(price)

        if not np.isfinite(px):
            return None

        ts = _normalize_ist_naive(
            timestamp
        )

        # Mandatory session close.
        if ts.time() >= self.square_off_time:
            return StrategySignal(
                action=SignalAction.EXIT,
                symbol=self.symbol,
                timestamp=ts,
                price=px,
                reason="AOU_OSS_TIME_SQUARE_OFF",
            )

        # Time stop.
        if self._time_stop_due(ts):
            return StrategySignal(
                action=SignalAction.EXIT,
                symbol=self.symbol,
                timestamp=ts,
                price=px,
                reason="AOU_OSS_TIME_STOP",
            )

        # LONG position.
        if self.position == 1:

            if px <= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.stop_loss,
                    reason="AOU_OSS_STOP_LOSS",
                )

            if px >= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.target,
                    reason="AOU_OSS_PROFIT_TARGET",
                )

        # SHORT position.
        elif self.position == -1:

            if px >= self.stop_loss:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.stop_loss,
                    reason="AOU_OSS_STOP_LOSS",
                )

            if px <= self.target:
                return StrategySignal(
                    action=SignalAction.EXIT,
                    symbol=self.symbol,
                    timestamp=ts,
                    price=self.target,
                    reason="AOU_OSS_PROFIT_TARGET",
                )

        return None

    # ------------------------------------------------------------------
    # Debug / inspection
    # ------------------------------------------------------------------

    def get_state(self) -> Dict[str, Any]:
        """
        Return serializable state for logging/API/UI.
        """
        return {
            "strategy": "aou_oss",
            "symbol": self.symbol,
            "ready": self.state.ready,

            "rolling_vwap": self.state.rolling_vwap,
            "spread": self.state.spread,
            "equilibrium": self.state.equilibrium,

            "phi": self.state.phi,
            "kappa": self.state.kappa,
            "sigma": self.state.sigma,
            "stationary_sigma": (
                self.state.stationary_sigma
            ),

            "half_life_bars": (
                self.state.half_life_bars
            ),
            "half_life_minutes": (
                self.state.half_life_minutes
            ),

            "volatility_ratio": (
                self.state.volatility_ratio
            ),
            "volatility_passed": (
                self.state.volatility_passed
            ),

            "entry_boundary_long": (
                self.state.entry_boundary_long
            ),
            "entry_boundary_short": (
                self.state.entry_boundary_short
            ),

            "stop_boundary_long": (
                self.state.stop_boundary_long
            ),
            "stop_boundary_short": (
                self.state.stop_boundary_short
            ),

            "l2_spread_bps": (
                self.state.l2_spread_bps
            ),
            "l2_passed": (
                self.state.l2_passed
            ),

            "position": self.position,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "target": self.target,
            "trades_today": self.trades_today,

            "last_timestamp": (
                self.state.last_timestamp.isoformat()
                if self.state.last_timestamp
                else None
            ),
        }


# Canonical alias
AOUOSSStrategy = AouOssStrategy