"""
CRSD — Cross-Sectional Residual Shock Divergence (NSE intraday relative value).

Standalone strategy: depends ONLY on config.settings, monitoring.logger and
the BaseStrategy interface. It does not import (or share state with) any other
strategy, and it ships its own peer-context container and peer-group map.

IDEA
    r_i,t = b_m r_m,t + b_s r_s,t + g T_i,t + e_i,t
    A short-lived shock pushes stock i away from the value implied by its
    contemporaneous factor exposures relative to a basket of liquid peers.
    When the divergence is large, liquidity is healthy and there is no sign of
    a fresh regime break, the residual spread tends to partially converge.

PIPELINE (every 15m bar)
    1. Local-beta factor model: exponentially-weighted ridge on market,
       leave-one-out sector and a decaying "transient" sector-shock factor.
       Half-life H is chosen by pseudo out-of-sample error on the training
       window. The market beta is tilted by a Parkinson range-vol ratio
       (intrabar candle information).
    2. Residuals e_k,t for stock i and each peer; equal-risk peer basket b
       with regression hedge ratio lambda;  D_t = eps_i(h) - lambda * eps_b(h).
    3. Robust z-score  Z_t = (D - median) / (1.4826 MAD)  by time-of-day
       bucket (training window only), plus a cross-sectional robust z across
       the peer group as a confirmation filter.
    4. Regime gate: Bayesian online changepoint detection (BOCPD, Student-t
       predictive) on the standardised spread increments, and a long-memory
       (power-law kernel) volatility state; the top stress bucket is excluded.
    5. Liquidity gate: turnover percentile, Corwin-Schultz spread proxy on own
       leg and every hedge leg.
    6. Cost gate: expected convergence edge must exceed edge_cost_mult x the
       round-trip cost of ALL legs.
    7. Exits (earliest wins): |Z| <= exit_z, regime break, max hold, spread
       tail-quantile stop, loss cap, liquidity shock, session square-off.
       A daily spread-drawdown limit blocks new entries.
    8. Optional in-training grid search of entry_z / exit_z, and an in-sample
       "does the spread converge at all after costs" evidence gate that
       disables the strategy for the session if it fails (the falsification
       test from the research brief).

IMPORTANT LIMITS — READ BEFORE TRUSTING ANY NUMBER
    * CRSDConfig defaults are UNCALIBRATED research placeholders.
    * This is a PAIR/BASKET strategy but BaseStrategy is single-instrument.
      The signal is emitted for the target stock only; the hedge basket is
      exposed in `self.hedge_legs` (symbol -> signed notional multiple of the
      target leg) and in the signal reason. The repo's StrategyBacktester
      therefore measures the UNHEDGED target leg. Use `last_features` /
      `hedge_legs` for pair-level P&L research.
    * Spread-based exits only fire while peer bars keep arriving. Live use
      must keep the context fresh via CRSDPeerContext.update(symbol, bar).
    * Missing peer/market bars fail closed (no signal, windows reset).
    * Cash shorts are intraday (MIS) only; this strategy never holds overnight.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from config.settings import CRSDConfig, InstrumentConfig, StrategyConfig, settings
from monitoring.logger import logger
from strategy.base_strategy import BaseStrategy, SignalAction, StrategySignal

_LN2 = math.log(2.0)
_CS_K = 3.0 - 2.0 * math.sqrt(2.0)
_MAD_TO_SD = 1.4826


# =============================================================================
from data.sector_peer_manager import SectorPeerManager


# =============================================================================
# Peer context (own container, causal, fail-closed)
# =============================================================================
class CRSDPeerContext:
    """
    Bars for peers and the market index, keyed by exact bar timestamp.

        ctx = CRSDPeerContext(peers={"ICICIBANK": df, ...}, market=df_nifty)

    Each df needs: datetime, close (high/low/volume strongly recommended).
    Lookups require an EXACT timestamp match — a missing bar returns None and
    the strategy fails closed rather than using a stale price.
    """

    _COLS = ("open", "high", "low", "close", "volume")

    def __init__(self, peers: Dict[str, pd.DataFrame], market: Optional[pd.DataFrame] = None):
        self.frames: Dict[str, pd.DataFrame] = {s: self._prep(df) for s, df in peers.items()}
        self.market: Optional[pd.DataFrame] = self._prep(market) if market is not None else None

    @classmethod
    def _prep(cls, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()
        if "datetime" not in d.columns and isinstance(d.index, pd.DatetimeIndex):
            d["datetime"] = d.index
        if "close" not in d.columns:
            raise ValueError("CRSDPeerContext frames require a 'close' column")
        d["datetime"] = pd.to_datetime(d["datetime"])
        d = d.sort_values("datetime").drop_duplicates("datetime").set_index("datetime")
        for c in cls._COLS:
            if c not in d.columns:
                d[c] = d["close"] if c in ("open", "high", "low") else np.nan
        return d[list(cls._COLS)].astype(float)

    def symbols(self) -> List[str]:
        return list(self.frames.keys())

    def bar(self, symbol: str, ts) -> Optional[Dict[str, float]]:
        df = self.market if symbol == "__market__" else self.frames.get(symbol)
        if df is None:
            return None
        try:
            row = df.loc[pd.Timestamp(ts)]
        except KeyError:
            return None
        if isinstance(row, pd.DataFrame):
            row = row.iloc[-1]
        c = float(row["close"])
        if not (c > 0 and math.isfinite(c)):
            return None
        return {k: float(row[k]) for k in self._COLS}

    def market_bar(self, ts) -> Optional[Dict[str, float]]:
        return self.bar("__market__", ts)

    def update(self, symbol: str, bar: Dict[str, Any]) -> None:
        """Append/overwrite one live bar (symbol or '__market__')."""
        ts = pd.Timestamp(bar["datetime"])
        row = {c: float(bar.get(c, bar["close"] if c in ("open", "high", "low") else np.nan))
               for c in self._COLS}
        new = pd.DataFrame([row], index=pd.DatetimeIndex([ts], name="datetime"))
        if symbol == "__market__":
            base = self.market if self.market is not None else new.iloc[0:0]
            self.market = pd.concat([base[base.index != ts], new]).sort_index()
        else:
            base = self.frames.get(symbol, new.iloc[0:0])
            self.frames[symbol] = pd.concat([base[base.index != ts], new]).sort_index()


def _read_cached_15m(
    sym: str,
    cache_dir: Path,
    latest_required_timestamp: Optional[datetime] = None,
) -> Optional[pd.DataFrame]:
    """
    Read a cached 15-minute frame only when it is structurally valid
    and fresh enough for the current CRSD evaluation.

    A stale cache is rejected so build_crsd_context() can fetch fresh
    data from Kite. No stale frame is returned to the strategy.
    """
    from data.historical_loader import (
        HistoricalDataLoader,
    )

    for name in (
        f"{sym}_15m.csv",
        f"{sym}50_15m.csv",
    ):
        cache_file = cache_dir / name

        if not cache_file.exists():
            continue

        try:
            df, _ = (
                HistoricalDataLoader
                .load_cached_data_with_validation(
                    cache_file
                )
            )

            if df is None or df.empty:
                continue

            required = {
                "datetime",
                "open",
                "high",
                "low",
                "close",
                "volume",
            }

            if not required.issubset(df.columns):
                logger.warning(
                    "CRSD cache rejected for %s: "
                    "missing required columns.",
                    sym,
                )
                continue

            df = df.copy()

            df["datetime"] = pd.to_datetime(
                df["datetime"],
                errors="coerce",
            )

            if df["datetime"].isna().any():
                logger.warning(
                    "CRSD cache rejected for %s: "
                    "invalid timestamps.",
                    sym,
                )
                continue

            df = (
                df
                .sort_values("datetime")
                .drop_duplicates(
                    subset="datetime",
                    keep="last",
                )
                .reset_index(drop=True)
            )

            latest_cached = df["datetime"].max()

            if latest_required_timestamp is not None:
                required_ts = pd.Timestamp(
                    latest_required_timestamp
                )

                if (
                    required_ts.tzinfo is not None
                    and latest_cached.tzinfo is None
                ):
                    required_ts = (
                        required_ts
                        .tz_convert("Asia/Kolkata")
                        .tz_localize(None)
                    )

                elif (
                    required_ts.tzinfo is None
                    and latest_cached.tzinfo is not None
                ):
                    latest_cached = (
                        latest_cached
                        .tz_convert("Asia/Kolkata")
                        .tz_localize(None)
                    )

                if latest_cached < required_ts:
                    logger.warning(
                        "CRSD cache rejected as stale for %s: "
                        "latest=%s required>=%s",
                        sym,
                        latest_cached,
                        required_ts,
                    )
                    continue

            return df

        except Exception as exc:
            logger.debug(
                "CRSD cache validation failed for %s: %s",
                sym,
                exc,
            )

    return None


def build_crsd_context(
    symbol: str,
    peers: Optional[Sequence[str]] = None,
    market_symbol: str = "NIFTY",
    cache_dir: Optional[Path] = None,
    kite_client: Optional[Any] = None,
    days: int = 45,
) -> Optional[CRSDPeerContext]:
    """
    Build a context from cached 15m CSVs (data/cache/<SYM>_15m.csv), optionally
    topping up from Kite. Returns None when the market frame or fewer than two
    peers are available. Never fabricates data.
    """
    c_dir = cache_dir or (settings.base_dir / "data" / "cache")
    sym = symbol.strip().upper()
    if peers is not None:
        peer_syms = list(peers)
    else:
        sec = SectorPeerManager.get_sector_for_symbol(sym)
        if sec is None:
            return None
        peer_syms = [m for m in sec.constituents if m != sym][:8]

    def load(
        sym: str,
    ) -> Optional[pd.DataFrame]:
        latest_required_timestamp = None

        try:
            from data.historical_loader import (
                HistoricalDataLoader,
            )

            latest_required_timestamp = (
                HistoricalDataLoader
                .get_latest_completed_candle_start(
                    now_ist_naive()
                )
            )
        except Exception as exc:
            logger.debug(
                "CRSD latest completed candle lookup failed: %s",
                exc,
            )

        df = _read_cached_15m(
            sym,
            c_dir,
            latest_required_timestamp=(
                latest_required_timestamp
            ),
        )

        if df is not None:
            return df

        if kite_client is None:
            return None
        try:
            from data.historical_loader import HistoricalDataLoader
            from data.instrument_resolver import instrument_resolver
            from data.time_utils import now_ist_naive

            tok = instrument_resolver.resolve_token(sym, exchange="NSE", kite_client=kite_client)
            if not tok:
                return None
            today = now_ist_naive().date()
            out = HistoricalDataLoader.fetch_real_data(
                kite_client=kite_client, instrument_token=tok,
                start_date=today - timedelta(days=days), end_date=today,
                interval="15minute", cache_path=c_dir / f"{sym}_15m.csv",
            )
            return out if out is not None and not out.empty else None
        except Exception as exc:
            logger.debug(f"CRSD kite fetch failed for {sym}: {exc}")
            return None

    mkt = load(market_symbol)
    if mkt is None:
        return None
    frames = {s: df for s in peer_syms if (df := load(s)) is not None}
    if len(frames) < 2:
        return None
    return CRSDPeerContext(peers=frames, market=mkt)


# =============================================================================
# Numerical building blocks
# =============================================================================
def parkinson_var(high: float, low: float) -> float:
    if not (high >= low > 0) or not math.isfinite(high) or not math.isfinite(low):
        return 0.0
    return math.log(high / low) ** 2 / (4.0 * _LN2)


def corwin_schultz_spread(h1, l1, h2, l2):
    """Corwin-Schultz (2012) high-low spread estimator, clipped at 0. Vectorised."""
    h1, l1, h2, l2 = (np.asarray(x, dtype=float) for x in (h1, l1, h2, l2))
    with np.errstate(all="ignore"):
        beta = np.log(h1 / l1) ** 2 + np.log(h2 / l2) ** 2
        gamma = np.log(np.maximum(h1, h2) / np.minimum(l1, l2)) ** 2
        alpha = (np.sqrt(2 * beta) - np.sqrt(beta)) / _CS_K - np.sqrt(gamma / _CS_K)
        s = 2.0 * (np.exp(alpha) - 1.0) / (1.0 + np.exp(alpha))
    return np.clip(s, 0.0, None)


def _mad_sigma(x) -> float:
    a = np.asarray(x, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return 0.0
    med = np.median(a)
    return float(_MAD_TO_SD * np.median(np.abs(a - med)))


def _wridge(X: np.ndarray, y: np.ndarray, w: np.ndarray, lam: float) -> np.ndarray:
    Xw = X * w[:, None]
    A = X.T @ Xw + lam * np.eye(X.shape[1])
    return np.linalg.solve(A, Xw.T @ y)


def _last_monthly_weekday(d: date, weekday: int) -> date:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


class BOCPD:
    """
    Bayesian online changepoint detection (Adams & MacKay 2007) with a
    Normal-Inverse-Gamma prior (Student-t predictive) and constant hazard.
    update(x) returns P(run length <= recent_window) — the probability that a
    regime break happened within the last few observations.
    """

    def __init__(self, hazard: float, max_run: int, kappa0: float, alpha0: float,
                 beta0: float, recent_window: int, mu0: float = 0.0):
        self.H = float(hazard)
        self.max_run = int(max_run)
        self.w = int(recent_window)
        self.mu0, self.kappa0, self.alpha0, self.beta0 = mu0, kappa0, alpha0, beta0
        r = np.arange(self.max_run + 2, dtype=float)
        a = alpha0 + 0.5 * r
        self._lg = np.array([math.lgamma(x + 0.5) - math.lgamma(x) for x in a])
        self.reset()

    def reset(self) -> None:
        self.R = np.array([1.0])
        self.mu = np.array([self.mu0])
        self.kappa = np.array([self.kappa0])
        self.alpha = np.array([self.alpha0])
        self.beta = np.array([self.beta0])

    def update(self, x: float) -> float:
        n = len(self.R)
        s2 = 2.0 * self.beta * (self.kappa + 1.0) / self.kappa
        logp = (self._lg[:n] - 0.5 * np.log(math.pi * s2)
                - (self.alpha + 0.5) * np.log1p((x - self.mu) ** 2 / s2))
        pred = np.exp(logp - logp.max())
        growth = self.R * pred * (1.0 - self.H)
        cp = float(np.sum(self.R * pred * self.H))
        newR = np.concatenate(([cp], growth))
        tot = newR.sum()
        if not np.isfinite(tot) or tot <= 0:
            self.reset()
            return 0.0
        newR /= tot
        mu_n = (self.kappa * self.mu + x) / (self.kappa + 1.0)
        beta_n = self.beta + self.kappa * (x - self.mu) ** 2 / (2.0 * (self.kappa + 1.0))
        self.mu = np.concatenate(([self.mu0], mu_n))
        self.kappa = np.concatenate(([self.kappa0], self.kappa + 1.0))
        self.alpha = np.concatenate(([self.alpha0], self.alpha + 0.5))
        self.beta = np.concatenate(([self.beta0], beta_n))
        if len(newR) > self.max_run + 1:
            newR[self.max_run] += newR[self.max_run + 1:].sum()
            newR = newR[: self.max_run + 1]
            self.mu, self.kappa = self.mu[: self.max_run + 1], self.kappa[: self.max_run + 1]
            self.alpha, self.beta = self.alpha[: self.max_run + 1], self.beta[: self.max_run + 1]
        self.R = newR
        return float(self.R[: self.w + 1].sum())


class VolMemory:
    """Power-law long-memory variance:  V_t = sum_l w_l x_{t-l+1}^2,  w_l ~ l^-exponent."""

    def __init__(self, length: int, exponent: float):
        self.kernel = np.arange(1, length + 1, dtype=float) ** (-exponent)
        self.buf: Deque[float] = deque(maxlen=length)

    def update(self, x2: float) -> float:
        self.buf.appendleft(float(x2))
        n = len(self.buf)
        k = self.kernel[:n]
        return float(np.dot(k, np.fromiter(self.buf, dtype=float, count=n)) / k.sum())


def _pctl_rank(sorted_arr: np.ndarray, v: float) -> float:
    if sorted_arr.size == 0:
        return 1.0
    return float(np.searchsorted(sorted_arr, v, side="right")) / sorted_arr.size


# =============================================================================
# Strategy
# =============================================================================
class CRSDStrategy(BaseStrategy):
    def __init__(
        self,
        instrument: InstrumentConfig,
        strategy_config: StrategyConfig = settings.strategy,
        ctx: Optional[CRSDPeerContext] = None,
        crsd: Optional[CRSDConfig] = None,
    ):
        super().__init__(instrument.symbol)
        self.instrument = instrument
        self.config = strategy_config
        self.ctx = ctx
        self.p = crsd or settings.crsd

        self.warm_history = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        self.today_bars: List[dict] = []
        self.current_date: Optional[date] = None
        self.model: Optional[dict] = None
        self.disabled_reason: str = "no_context"
        self.calibration: Dict[str, Any] = {}

        # live/trade state
        self.hedge_legs: Dict[str, float] = {}
        self.risk_scale: float = 1.0
        self.last_features: Dict[str, Any] = {}
        self.daily_pnl_bps: float = 0.0
        self._entry: Optional[dict] = None
        self._conf_count: int = 0
        self._conf_sign: int = 0
        self._liq_shock_n: int = 0
        self._init_stream_state()

    def seed_context(self, historical_bars: pd.DataFrame) -> None:
        required_cols = [
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]

        if historical_bars is None or historical_bars.empty:
            self.warm_history = pd.DataFrame(columns=required_cols)
            return

        h = historical_bars.copy()

        missing_cols = [
            c
            for c in required_cols
            if c not in h.columns
        ]

        if missing_cols:
            logger.warning(
                "CRSD historical context rejected for %s: "
                "missing required columns: %s",
                self.symbol,
                missing_cols,
            )
            self.warm_history = pd.DataFrame(
                columns=required_cols
            )
            self.disabled_reason = (
                "missing_required_historical_columns"
            )
            return

        h = h[required_cols].copy()

        numeric_cols = [
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]

        for c in numeric_cols:
            h[c] = pd.to_numeric(
                h[c],
                errors="coerce",
            )

        if h[required_cols].isna().any().any():
            logger.warning(
                "CRSD historical context rejected for %s: "
                "required OHLCV data contains null/invalid values.",
                self.symbol,
            )
            self.warm_history = pd.DataFrame(
                columns=required_cols
            )
            self.disabled_reason = (
                "invalid_required_historical_data"
            )
            return

        if (
            (h["open"] <= 0).any()
            or (h["high"] <= 0).any()
            or (h["low"] <= 0).any()
            or (h["close"] <= 0).any()
            or (h["volume"] < 0).any()
        ):
            logger.warning(
                "CRSD historical context rejected for %s: "
                "non-positive OHLC or negative volume detected.",
                self.symbol,
            )
            self.warm_history = pd.DataFrame(
                columns=required_cols
            )
            self.disabled_reason = (
                "invalid_ohlcv_values"
            )
            return

        if (
            (h["high"] < h["low"]).any()
            or (h["high"] < h["open"]).any()
            or (h["high"] < h["close"]).any()
            or (h["low"] > h["open"]).any()
            or (h["low"] > h["close"]).any()
        ):
            logger.warning(
                "CRSD historical context rejected for %s: "
                "invalid OHLC geometry detected.",
                self.symbol,
            )
            self.warm_history = pd.DataFrame(
                columns=required_cols
            )
            self.disabled_reason = (
                "invalid_ohlc_geometry"
            )
            return

        self.warm_history = (
            h[required_cols]
            .sort_values("datetime")
            .drop_duplicates(
                subset="datetime",
                keep="last",
            )
            .reset_index(drop=True)
        )

    def reset_session(self, session_date: date):
        self.current_date = session_date
        self.position = 0
        self.entry_price = self.stop_loss = self.target = self.initial_risk_dist = 0.0
        self.trailing_breakeven_active = False
        self.trades_today = 0
        self.today_bars = []
        self.hedge_legs = {}
        self.risk_scale = 1.0
        self.daily_pnl_bps = 0.0
        self._entry = None
        self._conf_count = self._conf_sign = self._liq_shock_n = 0
        self.last_features = {}
        self._fit(session_date)

    def register_trade_exit(self):
        super().register_trade_exit()
        self._entry = None
        self.hedge_legs = {}

    # ------------------------------------------------------------------ state
    def _init_stream_state(self) -> None:
        self._last_close: Dict[str, Optional[float]] = {}
        self._e_hist: List[Deque[float]] = []
        self._T: np.ndarray = np.zeros(0)
        self._pv: Dict[str, Deque[float]] = {}
        self._prev_own_hl: Optional[Tuple[float, float]] = None
        self._bocpd: Optional[BOCPD] = None
        self._vm_d: Optional[VolMemory] = None
        self._vm_m: Optional[VolMemory] = None

    def _stream_gap(self) -> None:
        """Data gap: drop every rolling window; BOCPD/vol-memory keep their history."""
        m = self.model
        if not m:
            return
        K = len(m["syms"])
        self._last_close = {s: None for s in m["syms"] + ["__market__"]}
        self._e_hist = [deque(maxlen=self.p.horizon_bars) for _ in range(K)]
        self._T = np.zeros(K)
        self._prev_own_hl = None
        self._conf_count = self._conf_sign = 0

    # -------------------------------------------------------------------- fit
    def _own_frame(self, session_date: date) -> pd.DataFrame:
        w = self.warm_history
        if w is None or w.empty:
            return pd.DataFrame()
        d = w.copy()
        d["datetime"] = pd.to_datetime(d["datetime"])
        d = d[d["datetime"] < pd.Timestamp(session_date)]
        d = d.sort_values("datetime").drop_duplicates("datetime").set_index("datetime")
        return d[["open", "high", "low", "close", "volume"]].astype(float)

    def _fit(self, session_date: date) -> None:
        self.model = None
        self._init_stream_state()
        p = self.p
        if self.ctx is None:
            self.disabled_reason = "no_context"
            return
        if self.ctx.market is None:
            self.disabled_reason = "no_market_frame"
            return
        if (p.skip_expiry_day and session_date.weekday() == p.expiry_weekday
                and session_date == _last_monthly_weekday(session_date, p.expiry_weekday)):
            self.disabled_reason = "expiry_day"
            return
        own = self._own_frame(session_date)
        if own.empty:
            self.disabled_reason = "no_history"
            return
        cut = pd.Timestamp(session_date)

        peers: List[str] = []
        for s in self.ctx.symbols():
            if s == self.symbol:
                continue
            f = self.ctx.frames[s]
            f = f[f.index < cut]
            cov = len(own.index.intersection(f.index)) / max(len(own), 1)
            if cov >= p.min_coverage:
                peers.append(s)
        peers = peers[: p.group_cap]
        if len(peers) < p.min_peers:
            self.disabled_reason = f"insufficient_peers({len(peers)})"
            return

        mkt = self.ctx.market[self.ctx.market.index < cut]
        common = own.index.intersection(mkt.index)
        for s in peers:
            common = common.intersection(self.ctx.frames[s].index)
        days = sorted(set(common.date))[-p.train_days:]
        common = common[pd.Index(common.date).isin(days)]
        if len(common) < p.min_train_bars:
            self.disabled_reason = f"insufficient_training({len(common)})"
            return

        syms = [self.symbol] + peers
        K = len(syms)
        src = {self.symbol: own, **{s: self.ctx.frames[s] for s in peers}}

        def panel(col: str) -> pd.DataFrame:
            return pd.DataFrame({s: src[s][col].reindex(common) for s in syms}).astype(float)

        C, Hh, Ll, V = panel("close"), panel("high"), panel("low"), panel("volume")
        mC = mkt["close"].reindex(common).astype(float)
        mH, mL = mkt["high"].reindex(common).astype(float), mkt["low"].reindex(common).astype(float)
        dates = np.array(common.date)
        tods = np.array(common.strftime("%H:%M"))

        with np.errstate(all="ignore"):
            R = np.log(C).groupby(dates).diff()
            mR = np.log(mC).groupby(dates).diff()
        valid = (R.notna().all(axis=1) & mR.notna()).to_numpy()
        if valid.sum() < p.min_train_bars - 2 * len(days):
            self.disabled_reason = "insufficient_valid_returns"
            return

        Rv = R.to_numpy()
        mv = mR.to_numpy()
        # sector factor: leave-one-out mean of the group
        S = (Rv.sum(axis=1, keepdims=True) - Rv) / (K - 1)

        # Parkinson range-vol, rolling within day (local-beta tilt)
        def pv_frame(H_, L_):
            with np.errstate(all="ignore"):
                v = (np.log(H_ / L_) ** 2) / (4 * _LN2)
            v = v.where((H_ >= L_) & (L_ > 0)).fillna(0.0)
            return v.groupby(dates).transform(lambda x: x.rolling(p.local_beta_window, min_periods=1).mean())

        pv = pv_frame(Hh, Ll).to_numpy()
        pvm = pv_frame(mH.to_frame("m"), mL.to_frame("m")).to_numpy()[:, 0]
        ratio = np.ones(K)
        tilt = np.ones_like(Rv)
        if p.use_local_beta:
            with np.errstate(all="ignore"):
                rel = np.sqrt(pv) / np.sqrt(pvm)[:, None]
            for k in range(K):
                good = valid & np.isfinite(rel[:, k]) & (rel[:, k] > 0)
                ratio[k] = float(np.median(rel[good, k])) if good.sum() > 10 else 1.0
                with np.errstate(all="ignore"):
                    t = rel[:, k] / ratio[k]
                tilt[:, k] = np.clip(np.where(np.isfinite(t), t, 1.0), *p.local_beta_clip)

        # transient factor: decayed cumulative sector-minus-market innovation (lagged)
        var_m = float(np.var(mv[valid]))
        if var_m <= 0:
            self.disabled_reason = "degenerate_market"
            return
        bsm = np.array([float(np.cov(S[valid, k], mv[valid])[0, 1] / var_m) for k in range(K)])
        decay = 2.0 ** (-1.0 / p.transient_halflife_bars)
        Tf = np.zeros_like(Rv)
        day_codes, day_index = np.unique(dates, return_inverse=True)
        for k in range(K):
            u = S[:, k] - bsm[k] * np.nan_to_num(mv)
            for d_i in range(len(day_codes)):
                rows = np.where((day_index == d_i) & valid)[0]
                t_state = 0.0
                for r_ in rows:
                    Tf[r_, k] = t_state
                    t_state = decay * (t_state + u[r_])

        Xs = [np.column_stack([np.nan_to_num(mv) * tilt[:, k], S[:, k], Tf[:, k]]) for k in range(K)]
        Y = np.nan_to_num(Rv)
        rows_v = np.where(valid)[0]
        D_days = len(day_codes)

        def fit_all(H_days: float, row_mask: np.ndarray, anchor_day: int) -> np.ndarray:
            ri = np.where(row_mask)[0]
            age = anchor_day - day_index[ri]
            w = 2.0 ** (-np.clip(age, 0, None) / H_days)
            return np.stack([_wridge(Xs[k][ri], Y[ri, k], w, p.ridge_lambda) for k in range(K)])

        best_H = p.halflife_grid_days[min(1, len(p.halflife_grid_days) - 1)]
        if D_days >= 6 and len(p.halflife_grid_days) > 1:
            cutoff = int(0.7 * D_days)
            tr_mask = valid & (day_index < cutoff)
            te = np.where(valid & (day_index >= cutoff))[0]
            if tr_mask.sum() > 60 and te.size > 20:
                best_err = np.inf
                for H_days in p.halflife_grid_days:
                    B = fit_all(H_days, tr_mask, cutoff - 1)
                    err = 0.0
                    for k in range(K):
                        res = Y[te, k] - Xs[k][te] @ B[k]
                        err += float(np.mean(res ** 2) / (np.var(Y[te, k]) + 1e-18))
                    if err < best_err:
                        best_err, best_H = err, H_days
        beta = fit_all(best_H, valid, D_days - 1)

        E = np.full_like(Y, np.nan)
        for k in range(K):
            E[valid, k] = Y[valid, k] - np.einsum("ij,j->i", Xs[k][valid], beta[k])

        # equal-risk basket from residual correlation with the target
        Ev = E[valid]
        sig_e = np.array([_mad_sigma(Ev[:, k]) for k in range(K)])
        corr = np.array([np.corrcoef(Ev[:, 0], Ev[:, k])[0, 1] if sig_e[k] > 0 else np.nan
                         for k in range(K)])
        order = [k for k in np.argsort(-np.nan_to_num(corr, nan=-9)) if k != 0]
        sel = [k for k in order if np.isfinite(corr[k]) and corr[k] >= p.min_peer_corr][: p.max_peers]
        if len(sel) < p.min_peers or sig_e[0] <= 0:
            self.disabled_reason = f"pair_ineligible(peers={len(sel)})"
            return
        w_vec = np.zeros(K)
        inv = np.array([1.0 / sig_e[k] for k in sel])
        w_vec[sel] = inv / inv.sum()
        eb = Ev @ w_vec
        var_eb = float(np.var(eb))
        if var_eb <= 0:
            self.disabled_reason = "degenerate_basket"
            return
        lam = float(np.clip(np.cov(Ev[:, 0], eb)[0, 1] / var_eb, *p.hedge_ratio_clip))
        D1 = Ev[:, 0] - lam * eb
        sigma_d = _mad_sigma(D1)
        if sigma_d <= 1e-9:
            self.disabled_reason = "degenerate_spread"
            return

        # D(h) on training bars, rolling within day
        idx_valid = common[valid]
        d_ser = pd.Series(D1, index=idx_valid)
        d_dates = np.array(idx_valid.date)
        Dh = d_ser.groupby(d_dates).rolling(p.horizon_bars, min_periods=p.horizon_bars).sum()
        Dh = Dh.reset_index(level=0, drop=True).reindex(idx_valid)
        d_tods = np.array(idx_valid.strftime("%H:%M"))
        ok = Dh.notna().to_numpy()
        g_med = float(np.median(Dh[ok]))
        g_sig = max(_mad_sigma(Dh[ok]), 1e-9)
        tod_stats: Dict[str, Tuple[float, float]] = {}
        for t in np.unique(d_tods[ok]):
            sel_t = ok & (d_tods == t)
            if sel_t.sum() >= 8:
                tod_stats[t] = (float(np.median(Dh[sel_t])), max(_mad_sigma(Dh[sel_t]), 0.2 * g_sig))

        # tail quantile of spread moves over the max-hold horizon (heavy-tail stop)
        moves: List[float] = []
        for dd in np.unique(d_dates):
            seg = Dh[(d_dates == dd)].dropna().to_numpy()
            if len(seg) > p.max_hold_bars:
                moves.extend(np.abs(seg[p.max_hold_bars:] - seg[:-p.max_hold_bars]).tolist())
        tail_move = float(np.quantile(moves, p.tail_quantile)) if len(moves) >= 20 else 3.0 * g_sig
        spread_stop = max(tail_move, p.stop_sigma * g_sig)

        # liquidity thresholds (own + selected hedge legs)
        turnover = (C * V).to_numpy()
        liq_min: Dict[str, Dict[str, float]] = {}
        liq_med: Dict[str, Dict[str, float]] = {}
        liq_glob: Dict[str, Tuple[float, float]] = {}
        for k in [0] + sel:
            col = turnover[:, k]
            gq = float(np.nanquantile(col, p.liq_turnover_pctl)) if np.isfinite(col).any() else np.nan
            gm = float(np.nanmedian(col)) if np.isfinite(col).any() else np.nan
            liq_glob[syms[k]] = (gq, gm)
            liq_min[syms[k]], liq_med[syms[k]] = {}, {}
            for t in np.unique(tods):
                m_t = (tods == t) & np.isfinite(col)
                if m_t.sum() >= 8:
                    liq_min[syms[k]][t] = float(np.quantile(col[m_t], p.liq_turnover_pctl))
                    liq_med[syms[k]][t] = float(np.median(col[m_t]))
        h1, l1 = Hh[syms[0]].shift(1).to_numpy(), Ll[syms[0]].shift(1).to_numpy()
        cs_hist = corwin_schultz_spread(h1, l1, Hh[syms[0]].to_numpy(), Ll[syms[0]].to_numpy())
        same_day = np.r_[False, dates[1:] == dates[:-1]]
        cs_hist = cs_hist[same_day & np.isfinite(cs_hist)]
        spread_thr = float(np.quantile(cs_hist, p.liq_spread_pctl)) if cs_hist.size >= 20 else np.nan

        # ---- convergence evidence + optional in-training calibration
        day_series: List[Tuple[np.ndarray, np.ndarray]] = []
        for dd in np.unique(d_dates):
            m_d = (d_dates == dd) & ok
            if m_d.sum() == 0:
                continue
            dv = Dh[m_d].to_numpy()
            zt = np.array([(dv[i] - tod_stats.get(t, (g_med, g_sig))[0]) / tod_stats.get(t, (g_med, g_sig))[1]
                           for i, t in enumerate(d_tods[m_d])])
            day_series.append((dv, zt))
        cost_ret = p.round_trip_cost_bps_per_leg * (1.0 + lam) * 1e-4
        entry_z, exit_z = p.entry_z, p.exit_z
        calib: Dict[str, Any] = {"mode": "fixed"}
        if p.auto_calibrate:
            best = None
            for ez in p.entry_z_grid:
                for xz in p.exit_z_grid:
                    if xz >= ez:
                        continue
                    st = self._simulate(day_series, ez, xz, p.max_hold_bars, cost_ret)
                    if st["n"] < p.calib_min_events or not (st["half1"] > 0 and st["half2"] > 0):
                        continue
                    if best is None or st["mean_net"] > best[0]:
                        best = (st["mean_net"], ez, xz)
            if best is not None:
                entry_z, exit_z = best[1], best[2]
                calib = {"mode": "calibrated", "entry_z": entry_z, "exit_z": exit_z}
            else:
                calib = {"mode": "calibration_failed_kept_defaults"}
        stats = self._simulate(day_series, entry_z, exit_z, p.max_hold_bars, cost_ret)
        calib.update(stats)
        self.calibration = calib
        if p.require_convergence_evidence:
            if (stats["n"] < p.calib_min_events or stats["hit"] < p.min_hit_rate
                    or stats["mean_net"] * 1e4 < p.min_net_bps):
                self.disabled_reason = (
                    f"no_convergence_evidence(n={stats['n']},hit={stats['hit']:.2f},"
                    f"net_bps={stats['mean_net'] * 1e4:.1f})")
                return

        # ---- warm regime detectors on the training spread / market series
        d_std = D1 / sigma_d
        mv_valid = mv[valid]
        self._bocpd = BOCPD(p.bocpd_hazard, p.bocpd_max_run, p.bocpd_kappa0, p.bocpd_alpha0,
                            p.bocpd_beta0, p.bocpd_recent_window)
        self._vm_d = VolMemory(p.vm_len, p.vm_exponent)
        self._vm_m = VolMemory(p.vm_len, p.vm_exponent)
        vd_hist, vm_hist = [], []
        for i, x in enumerate(d_std):
            vd_hist.append(self._vm_d.update(x * x))
            vm_hist.append(self._vm_m.update(mv_valid[i] ** 2))
        for x in d_std[-p.bocpd_warm_bars:]:
            self._bocpd.update(float(x))

        self.model = dict(
            syms=syms, beta=beta, bsm=bsm, ratio=ratio, sel=sel, w=w_vec, lam=lam,
            sigma_d=sigma_d, tod_stats=tod_stats, g_med=g_med, g_sig=g_sig,
            spread_stop=spread_stop, liq_min=liq_min, liq_med=liq_med, liq_glob=liq_glob,
            spread_thr=spread_thr, vd_sorted=np.sort(vd_hist), vm_sorted=np.sort(vm_hist),
            entry_z=entry_z, exit_z=exit_z, best_H=best_H, decay=decay, corr=corr,
        )
        self.disabled_reason = ""
        self._stream_gap()
        logger.info(
            f"CRSD[{self.symbol}] ready: peers={[syms[k] for k in sel]} lam={lam:.2f} "
            f"H={best_H}d entry_z={entry_z} exit_z={exit_z} hit={stats['hit']:.2f} n={stats['n']}")

    @staticmethod
    def _simulate(day_series, ez: float, xz: float, hold: int, cost_ret: float) -> Dict[str, float]:
        """In-sample event study of spread convergence (training window only)."""
        gross: List[float] = []
        for dv, zt in day_series:
            i = 0
            while i < len(dv) - 1:
                if abs(zt[i]) >= ez:
                    direction = -np.sign(zt[i])
                    j = i + 1
                    while j < len(dv) - 1 and abs(zt[j]) > xz and (j - i) < hold:
                        j += 1
                    gross.append(float(direction * (dv[j] - dv[i])))
                    i = j + 1
                else:
                    i += 1
        n = len(gross)
        if n == 0:
            return dict(n=0, hit=0.0, mean_net=-1.0, half1=-1.0, half2=-1.0)
        g = np.asarray(gross)
        net = g - cost_ret
        h = n // 2
        return dict(n=n, hit=float(np.mean(g > 0)), mean_net=float(net.mean()),
                    half1=float(net[: max(h, 1)].mean()), half2=float(net[h:].mean()))

    # --------------------------------------------------------------- streaming
    def _ingest(self, candle: dict) -> Optional[dict]:
        """Advance every rolling window by one bar; return features (or None)."""
        m, p, ctx = self.model, self.p, self.ctx
        ts = candle["datetime"]
        syms = m["syms"]
        K = len(syms)
        if not self._e_hist:
            self._stream_gap()

        own_close = float(candle["close"])
        own_hi = float(candle.get("high", own_close))
        own_lo = float(candle.get("low", own_close))
        bars: List[Optional[dict]] = [dict(close=own_close, high=own_hi, low=own_lo,
                                           volume=float(candle.get("volume", np.nan)))]
        for s in syms[1:]:
            bars.append(ctx.bar(s, ts))
        mb = ctx.market_bar(ts)
        if mb is None or any(b is None for b in bars) or not (own_close > 0):
            self._stream_gap()
            return None

        # Parkinson variance windows (today only)
        for s, b in zip(syms, bars):
            self._pv.setdefault(s, deque(maxlen=p.local_beta_window)).append(parkinson_var(b["high"], b["low"]))
        self._pv.setdefault("__market__", deque(maxlen=p.local_beta_window)).append(
            parkinson_var(mb["high"], mb["low"]))

        closes = [b["close"] for b in bars]
        prevs = [self._last_close.get(s) for s in syms]
        prev_m = self._last_close.get("__market__")
        for s, c in zip(syms, closes):
            self._last_close[s] = c
        self._last_close["__market__"] = mb["close"]
        own_prev_hl, self._prev_own_hl = self._prev_own_hl, (own_hi, own_lo)
        if any(x is None for x in prevs) or prev_m is None:
            return None

        r = np.log(np.array(closes) / np.array(prevs))
        m_r = math.log(mb["close"] / prev_m)
        s_fac = (r.sum() - r) / (K - 1)
        pvm = float(np.mean(self._pv["__market__"]))
        tilt = np.ones(K)
        if p.use_local_beta and pvm > 0:
            for k, s in enumerate(syms):
                pk = float(np.mean(self._pv[s]))
                tilt[k] = float(np.clip((math.sqrt(pk) / math.sqrt(pvm)) / m["ratio"][k],
                                        *p.local_beta_clip)) if pk > 0 else 1.0
        B = m["beta"]
        e = r - (B[:, 0] * m_r * tilt + B[:, 1] * s_fac + B[:, 2] * self._T)
        self._T = m["decay"] * (self._T + (s_fac - m["bsm"] * m_r))
        for k in range(K):
            self._e_hist[k].append(float(e[k]))

        w, lam, sigma_d = m["w"], m["lam"], m["sigma_d"]
        d1 = float(e[0] - lam * float(w @ e))
        d_std = d1 / sigma_d
        cp_recent = self._bocpd.update(d_std)
        vd = self._vm_d.update(d_std * d_std)
        vmk = self._vm_m.update(m_r * m_r)
        stress = max(_pctl_rank(m["vd_sorted"], vd), _pctl_rank(m["vm_sorted"], vmk))

        feats: Dict[str, Any] = dict(ts=ts, ready=False, cp_recent=cp_recent, stress=stress, d_std=d_std)
        if min(len(h) for h in self._e_hist) < p.horizon_bars:
            self.last_features = feats
            return feats

        eps = np.array([sum(h) for h in self._e_hist])
        D = float(eps[0] - lam * float(w @ eps))
        tod = pd.Timestamp(ts).strftime("%H:%M")
        med, sig = m["tod_stats"].get(tod, (m["g_med"], m["g_sig"]))
        z = (D - med) / sig
        z_cs = None
        if K >= p.cs_min_group:
            cmed = float(np.median(eps))
            cmad = _MAD_TO_SD * float(np.median(np.abs(eps - cmed)))
            z_cs = (eps[0] - cmed) / cmad if cmad > 1e-12 else 0.0

        # liquidity
        turnover = own_close * bars[0]["volume"]
        liq_ok = True
        legs_ok = True
        for k in [0] + list(m["sel"]):
            s = syms[k]
            t_now = closes[k] * bars[k]["volume"]
            thr = m["liq_min"].get(s, {}).get(tod, m["liq_glob"][s][0])
            if not (np.isfinite(t_now) and np.isfinite(thr) and t_now >= thr):
                legs_ok = False
        spread_now = float("nan")
        if own_prev_hl is not None:
            spread_now = float(corwin_schultz_spread(own_prev_hl[0], own_prev_hl[1], own_hi, own_lo))
        if not (np.isfinite(spread_now) and np.isfinite(m["spread_thr"]) and spread_now <= m["spread_thr"]):
            liq_ok = False
        liq_ok = liq_ok and legs_ok
        med_t = m["liq_med"][syms[0]].get(tod, m["liq_glob"][syms[0]][1])
        shock_now = bool(np.isfinite(turnover) and np.isfinite(med_t) and turnover < p.liq_shock_frac * med_t)
        self._liq_shock_n = self._liq_shock_n + 1 if shock_now else 0
        spread_blowout = bool(np.isfinite(spread_now) and np.isfinite(m["spread_thr"])
                              and spread_now > 2.0 * m["spread_thr"])

        feats.update(ready=True, D=D, z=z, z_cs=z_cs, sigma_tod=sig, med_tod=med, liq_ok=liq_ok,
                     liq_shock=(self._liq_shock_n >= 2) or spread_blowout, spread=spread_now,
                     eps_i=float(eps[0]), tod=tod)
        self.last_features = feats
        return feats

    # ---------------------------------------------------------------- signals
    def on_candle(self, candle: dict, vwap: float) -> Optional[StrategySignal]:
        self.today_bars.append(candle)
        ts, price, t = candle["datetime"], float(candle["close"]), candle["datetime"].time()
        st = self._ingest(candle) if self.model is not None else None

        if self.position != 0:
            return self._manage(candle, st)
        if self.model is None or self.trades_today >= self.p.max_trades_per_day:
            return None
        if self.daily_pnl_bps <= -self.p.daily_dd_limit_bps:
            return None
        if not (self.p.entry_start <= t <= self.p.entry_end):
            return None
        if st is None or not st.get("ready"):
            return None

        p, m = self.p, self.model
        z = st["z"]
        sign = 1 if z > 0 else -1
        if abs(z) >= m["entry_z"]:
            self._conf_count = self._conf_count + 1 if sign == self._conf_sign else 1
            self._conf_sign = sign
        else:
            self._conf_count = 0
            return None
        if self._conf_count < p.confirm_bars:
            return None
        if st["cp_recent"] >= p.cp_entry_max:
            return None
        if st["stress"] >= p.vm_stress_pctl:
            return None
        if not st["liq_ok"]:
            return None
        if st["z_cs"] is not None and (np.sign(st["z_cs"]) != sign or abs(st["z_cs"]) < p.cs_confirm_z):
            return None

        sigma, med = st["sigma_tod"], st["med_tod"]
        gap = abs(st["D"] - med)
        edge_ret = p.capture_frac * (gap - m["exit_z"] * sigma)
        cost_bps = p.round_trip_cost_bps_per_leg * (1.0 + m["lam"])
        if edge_ret * 1e4 < p.edge_cost_mult * cost_bps or edge_ret * 1e4 < p.min_target_bps:
            return None

        d = -sign  # z>0 -> own leg rich vs basket -> SELL own, BUY hedges
        stop_ret = p.leg_stop_mult * m["spread_stop"]
        risk, reward = stop_ret * price, edge_ret * price
        if risk <= 0 or reward <= 0:
            return None

        self.risk_scale = 0.5 if st["stress"] >= p.vm_scale_pctl else 1.0
        # self.hedge_legs stores signed hedge notional weights relative to the target stock's notional
        # (e.g. +0.41 means hedge with 41% of target notional in this peer, NOT 0.41 shares).
        # PairPositionSizer converts these notional weights into actual share quantities.
        self.hedge_legs = {m["syms"][k]: float(-d * m["lam"] * m["w"][k]) for k in m["sel"]}
        self.hedge_notional_weights = dict(self.hedge_legs)
        self._entry = dict(bar=len(self.today_bars) - 1, D0=st["D"], z0=z, dir_spread=-sign,
                           ts=ts, entry_price=price)
        legs = ",".join(f"{s}:{v:+.2f}" for s, v in self.hedge_legs.items())
        return StrategySignal(
            action=SignalAction.BUY if d == 1 else SignalAction.SELL,
            symbol=self.symbol,
            timestamp=ts,
            price=price,
            stop_loss=price - d * risk,
            target=price + d * reward,
            hedge_legs=dict(self.hedge_legs),
            reason=(f"CRSD z={z:.2f} z_cs={'na' if st['z_cs'] is None else f'{st['z_cs']:.2f}'} "
                    f"edge={edge_ret * 1e4:.1f}bps cp={st['cp_recent']:.2f} stress={st['stress']:.2f} "
                    f"scale={self.risk_scale} hedge[{legs}]"),
        )

    def _book(self, pnl_bps: float) -> None:
        self.daily_pnl_bps += pnl_bps

    def _manage(self, candle: dict, st: Optional[dict]) -> Optional[StrategySignal]:
        ts, hi, lo, price = candle["datetime"], candle["high"], candle["low"], candle["close"]

        def mk(px: float, why: str, pnl_bps: Optional[float] = None) -> StrategySignal:
            if pnl_bps is None and self.entry_price:
                pnl_bps = self.position * (px / self.entry_price - 1.0) * 1e4
            self._book(pnl_bps or 0.0)
            return StrategySignal(
                action=SignalAction.EXIT,
                symbol=self.symbol,
                timestamp=ts,
                price=px,
                reason=why,
                hedge_legs=dict(self.hedge_legs),
            )

        # 1. Intraday session time square-off
        if ts.time() >= self.p.session_end:
            return mk(price, "TIME_SQUARE_OFF")

        # 2. Authoritative pair spread economic exits (target residual + hedge basket residual)
        e, m = self._entry, self.model
        if e is not None and m is not None and st is not None and st.get("ready"):
            p = self.p
            pnl_ret = e["dir_spread"] * (st["D"] - e["D0"])
            pnl_bps = pnl_ret * 1e4
            held = len(self.today_bars) - 1 - e["bar"]

            # Convergence exit (z-score reverts to normal equilibrium)
            if abs(st["z"]) <= m["exit_z"] or np.sign(st["z"]) != np.sign(e["z0"]):
                return mk(price, "CONVERGED", pnl_bps)

            # Pair spread stop (residual divergence expands beyond model spread stop)
            if pnl_ret <= -m["spread_stop"]:
                return mk(price, "SPREAD_STOP", pnl_bps)

            # Strategy loss cap
            if pnl_bps <= -p.strategy_loss_cap_bps:
                return mk(price, "LOSS_CAP", pnl_bps)

            # Regime break / liquidity shocks
            if st["cp_recent"] >= p.cp_exit:
                return mk(price, "REGIME_BREAK", pnl_bps)
            if st["liq_shock"]:
                return mk(price, "LIQUIDITY_SHOCK", pnl_bps)

            # Max holding period time stop
            if held >= p.max_hold_bars:
                return mk(price, "TIME_STOP", pnl_bps)

            return None

        # 3. Emergency fallback if peer context state is temporarily unready
        if self.position == 1:
            if lo <= self.stop_loss:
                return mk(self.stop_loss, "STOP_LOSS")
            if hi >= self.target:
                return mk(self.target, "PROFIT_TARGET")
        else:
            if hi >= self.stop_loss:
                return mk(self.stop_loss, "STOP_LOSS")
            if lo <= self.target:
                return mk(self.target, "PROFIT_TARGET")

        return None

    def on_tick(self, price: float, timestamp: datetime) -> Optional[StrategySignal]:
        if self.position == 0:
            return None
        mk = lambda px, why: StrategySignal(
            action=SignalAction.EXIT,
            symbol=self.symbol,
            timestamp=timestamp,
            price=px,
            reason=why,
            hedge_legs=dict(self.hedge_legs),
        )
        # CRSD is a market-neutral pair. Single-stock tick noise on the target leg must not
        # trigger premature single-leg stops. Authoritative exits are evaluated via spread
        # convergence and spread stops on candle closes.
        if timestamp.time() >= self.p.session_end:
            return mk(price, "TIME_SQUARE_OFF")
        return None