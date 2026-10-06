"""
Sector Impulse Transmission (SIT).

Trade a lagging peer after a volume-confirmed idiosyncratic impulse in a sector
leader, only where a stable lead-lag relationship exists in the training window.

Plugs into the existing BaseStrategy / StrategyBacktester contract unchanged.
Peer data (leader, market index, sector index) is passed via PeerContext:

    ctx = PeerContext(leader=df_leader, market=df_nifty, sector=df_sector)
    StrategyBacktester(strategy_factory=lambda: SectorImpulseStrategy(inst, ctx=ctx), ...)

Each df needs columns: datetime, close, volume (leader also: volume). Lookups are
causal (never past the current candle); fitting uses only bars before the
session date.

Cadence: Horizons are calibrated for 15-minute production candles (horizon_bars=2,
lag_max=2 corresponding to 30-min intraday lead-lag transmission window).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from config.settings import InstrumentConfig, StrategyConfig, settings
from monitoring.logger import logger
from strategy.base_strategy import BaseStrategy, SignalAction, StrategySignal


@dataclass
class SITConfig:
    horizon_bars: int = 2            # h: 2 x 15m bars = 30m residual-return window
    lag_max: int = 2                 # search k in 1..2 15m bars
    train_days: int = 20
    ridge_lambda: float = 1e-6
    min_rho: float = 0.10            # lead-lag stability floor
    z_leader_thr: float = 2.0
    vz_thr: float = 1.5
    idio_ratio_min: float = 0.6      # |resid leader| / |raw leader| (idiosyncratic share)
    gap_thr_sigma: float = 1.0       # |G| in laggard tod-sigma units
    response_frac_max: float = 0.5   # laggard already moved > this share of e_i -> skip
    round_trip_cost_bps: float = 8.0
    cost_ratio_max: float = 0.4      # cost / expected move
    stop_sigma: float = 1.0
    target_frac: float = 0.8
    target_cap_sigma: float = 3.0
    invalidation_frac: float = 0.5   # leader retraces this share of impulse -> exit
    time_stop_mult: float = 1.5
    panic_market_z: float = 2.5      # Calibrated market panic regime gate (z > 2.5 skips entry)
    entry_start: time = time(9, 30)
    entry_end: time = time(14, 45)
    session_end: time = time(15, 10)
    expiry_weekday: int = 1          # monthly expiry weekday (Tue); disabled that day
    max_trades_per_day: int = 1


class PeerContext:
    """Causal accessor over leader / market / sector frames."""

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
        """Aligned closes strictly before `ts` (leader volume kept)."""
        cut = pd.Timestamp(ts)
        out = pd.concat(
            {k: v["close"] for k, v in self.frames.items()}, axis=1, sort=False
        ).dropna()
        out["leader_vol"] = self.frames["leader"]["volume"].reindex(out.index) if "volume" in self.frames["leader"] else 0.0
        return out[out.index < cut]


def _ridge(X: np.ndarray, y: np.ndarray, lam: float) -> np.ndarray:
    return np.linalg.solve(X.T @ X + lam * np.eye(X.shape[1]), X.T @ y)


def _last_monthly_weekday(d: date, weekday: int) -> date:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


class SectorImpulseStrategy(BaseStrategy):
    def __init__(
        self,
        instrument: InstrumentConfig,
        strategy_config: StrategyConfig = settings.strategy,
        ctx: Optional[PeerContext] = None,
        sit: Optional[SITConfig] = None,
    ):
        super().__init__(instrument.symbol)
        self.instrument = instrument
        self.config = strategy_config
        self.ctx = ctx
        self.p = sit or SITConfig()

        self.warm_history = pd.DataFrame(columns=["datetime", "close"])
        self.today_bars: List[dict] = []
        self.current_date: Optional[date] = None
        self.model: Optional[dict] = None       # fit result; None => ineligible
        self.disabled_reason: str = "no_context"
        self.entry_bar: int = -1
        self.entry_impulse: float = 0.0
        self.entry_dir: int = 0

    # ------------------------------------------------------------------ fit
    def seed_context(self, historical_bars: pd.DataFrame) -> None:
        if historical_bars is None or historical_bars.empty:
            self.warm_history = pd.DataFrame(columns=["datetime", "close"])
        else:
            self.warm_history = historical_bars[["datetime", "close"]].copy()

    def reset_session(self, session_date: date):
        self.current_date = session_date
        self.position = 0
        self.entry_price = self.stop_loss = self.target = self.initial_risk_dist = 0.0
        self.trailing_breakeven_active = False
        self.trades_today = 0
        self.today_bars = []
        self.entry_bar = -1
        self._fit(session_date)

    def _fit(self, session_date: date) -> None:
        self.model = None
        if self.ctx is None:
            self.disabled_reason = "no_context"
            return
        if session_date.weekday() == self.p.expiry_weekday and \
                session_date == _last_monthly_weekday(session_date, self.p.expiry_weekday):
            self.disabled_reason = "expiry_day"
            return
        cut = pd.Timestamp(session_date)
        own = self.warm_history.copy()
        own["datetime"] = pd.to_datetime(own["datetime"])
        own = own[own["datetime"] < cut].set_index("datetime")["close"].rename("own")
        peers = self.ctx.before(cut)
        df = peers.join(own, how="inner").dropna()
        if df.empty:
            self.disabled_reason = "no_training_overlap"
            return
        days = sorted(set(df.index.date))[-self.p.train_days:]
        df = df[[d in set(days) for d in df.index.date]]
        if len(df) < 200:
            self.disabled_reason = "insufficient_training"
            return

        # keep returns within a day (no overnight jumps)
        day_key = pd.Series(df.index.date, index=df.index)
        rets = np.log(df[["leader", "market", "sector", "own"]]).groupby(day_key.values).diff().dropna()
        vol = df["leader_vol"].reindex(rets.index)
        X = rets[["market", "sector"]].values
        beta_l = _ridge(X, rets["leader"].values, self.p.ridge_lambda)
        beta_i = _ridge(X, rets["own"].values, self.p.ridge_lambda)
        res_l = pd.Series(rets["leader"].values - X @ beta_l, index=rets.index)
        res_i = pd.Series(rets["own"].values - X @ beta_i, index=rets.index)

        # lead-lag: corr(res_l[t-k], res_i[t]), stability across two halves
        best_k, best_rho, stable = 0, 0.0, False
        half = len(res_l) // 2
        for k in range(1, self.p.lag_max + 1):
            a, b = res_l.shift(k), res_i
            m = a.notna()
            rho = float(np.corrcoef(a[m], b[m])[0, 1])
            r1 = float(np.corrcoef(a[m].iloc[:half], b[m].iloc[:half])[0, 1])
            r2 = float(np.corrcoef(a[m].iloc[half:], b[m].iloc[half:])[0, 1])
            ok = np.sign(r1) == np.sign(r2) == np.sign(rho) and min(abs(r1), abs(r2)) >= self.p.min_rho / 2
            if abs(rho) > abs(best_rho):
                best_k, best_rho, stable = k, rho, bool(ok)
        if not stable or abs(best_rho) < self.p.min_rho:
            self.disabled_reason = f"pair_ineligible(rho={best_rho:.3f},k={best_k})"
            return

        h = self.p.horizon_bars
        tod = rets.index.strftime("%H:%M")
        rh_l = res_l.rolling(h).sum().dropna()
        rh_i = res_i.rolling(h).sum().dropna()
        sig_l = rh_l.groupby(rh_l.index.strftime("%H:%M")).std()
        sig_i = rh_i.groupby(rh_i.index.strftime("%H:%M")).std()
        lv = np.log1p(vol.clip(lower=0))
        lv_med = lv.groupby(tod).median()
        lv_mad = (lv - lv.groupby(tod).transform("median")).abs().groupby(tod).median().replace(0, np.nan)
        mkt_sig = rets["market"].rolling(h).sum().std()

        self.model = dict(
            beta_l=beta_l, beta_i=beta_i, k=best_k, rho=best_rho,
            sig_l=sig_l, sig_i=sig_i, sig_l_all=float(rh_l.std()), sig_i_all=float(rh_i.std()),
            lv_med=lv_med, lv_mad=lv_mad, mkt_sig=float(mkt_sig),
        )
        self.disabled_reason = ""
        logger.info(f"SIT[{self.symbol}] eligible: k={best_k} rho={best_rho:.3f}")

    # ------------------------------------------------------------- features
    def _features(self, candle: dict) -> Optional[dict]:
        m, h, ts = self.model, self.p.horizon_bars, candle["datetime"]
        if len(self.today_bars) <= h:
            return None
        own_now, own_then = candle["close"], self.today_bars[-1 - h]["close"]
        pts = {}
        for name in ("leader", "market", "sector"):
            now, then = self.ctx.close_back(name, ts, 0), self.ctx.close_back(name, ts, h)
            if now is None or then is None or then <= 0:
                return None
            pts[name] = np.log(now / then)
        R_i = np.log(own_now / own_then)
        r_l = pts["leader"] - m["beta_l"][0] * pts["market"] - m["beta_l"][1] * pts["sector"]
        r_i = R_i - m["beta_i"][0] * pts["market"] - m["beta_i"][1] * pts["sector"]
        tod = pd.Timestamp(ts).strftime("%H:%M")
        s_l = m["sig_l"].get(tod, m["sig_l_all"]) or m["sig_l_all"]
        s_i = m["sig_i"].get(tod, m["sig_i_all"]) or m["sig_i_all"]
        if not (s_l > 0 and s_i > 0):
            return None
        # leader volume shock (last bar)
        lv_now = np.log1p(max(float(self.ctx.frames["leader"]["volume"].iloc[
            self.ctx.frames["leader"].index.searchsorted(pd.Timestamp(ts), side="right") - 1]), 0.0))
        mad = m["lv_mad"].get(tod, np.nan)
        v_z = (lv_now - m["lv_med"].get(tod, lv_now)) / mad if mad and not np.isnan(mad) else 0.0
        e_i = m["rho"] * (s_i / s_l) * r_l
        return dict(Z_L=r_l / s_l, V_z=v_z, r_l=r_l, R_l=pts["leader"], r_i=r_i, e_i=e_i,
                    G=e_i - r_i, s_i=s_i, mkt_z=pts["market"] / m["mkt_sig"] if m["mkt_sig"] > 0 else 0.0)

    # --------------------------------------------------------------- signals
    def on_candle(self, candle: dict, vwap: float) -> Optional[StrategySignal]:
        self.today_bars.append(candle)
        ts, price, t = candle["datetime"], candle["close"], candle["datetime"].time()

        if self.position != 0:
            return self._manage(candle)
        if self.model is None or self.trades_today >= self.p.max_trades_per_day:
            return None
        if not (self.p.entry_start <= t <= self.p.entry_end):
            return None

        f = self._features(candle)
        if f is None or abs(f["mkt_z"]) > self.p.panic_market_z:
            return None
        if abs(f["Z_L"]) < self.p.z_leader_thr or f["V_z"] < self.p.vz_thr:
            return None
        if abs(f["R_l"]) > 0 and abs(f["r_l"]) / abs(f["R_l"]) < self.p.idio_ratio_min:
            return None
        G, s_i = f["G"], f["s_i"]
        if abs(G) / s_i < self.p.gap_thr_sigma:
            return None
        if abs(f["e_i"]) > 0 and f["r_i"] * np.sign(f["e_i"]) > self.p.response_frac_max * abs(f["e_i"]):
            return None
        if self.p.round_trip_cost_bps / (abs(G) * 1e4) > self.p.cost_ratio_max:
            return None

        d = 1 if G > 0 else -1
        risk = self.p.stop_sigma * s_i * price
        reward = min(self.p.target_frac * abs(G), self.p.target_cap_sigma * s_i) * price
        if risk <= 0 or reward <= 0:
            return None
        self.entry_bar = len(self.today_bars) - 1
        self.entry_impulse, self.entry_dir = f["r_l"], d
        return StrategySignal(
            action=SignalAction.BUY if d == 1 else SignalAction.SELL,
            symbol=self.symbol, timestamp=ts, price=price,
            stop_loss=price - d * risk, target=price + d * reward,
            reason=f"SIT gap={G*1e4:.1f}bps Z_L={f['Z_L']:.2f} V_z={f['V_z']:.2f} k={self.model['k']}",
        )

    def _manage(self, candle: dict) -> Optional[StrategySignal]:
        ts, hi, lo, price = candle["datetime"], candle["high"], candle["low"], candle["close"]
        mk = lambda px, why: StrategySignal(action=SignalAction.EXIT, symbol=self.symbol,
                                            timestamp=ts, price=px, reason=why)
        if self.position == 1:
            if lo <= self.stop_loss: return mk(self.stop_loss, "STOP_LOSS")
            if hi >= self.target: return mk(self.target, "PROFIT_TARGET")
        else:
            if hi >= self.stop_loss: return mk(self.stop_loss, "STOP_LOSS")
            if lo <= self.target: return mk(self.target, "PROFIT_TARGET")
        if candle["datetime"].time() >= self.p.session_end:
            return mk(price, "TIME_SQUARE_OFF")
        if self.entry_bar >= 0 and self.model:
            held = len(self.today_bars) - 1 - self.entry_bar
            if held >= max(1, int(round(self.p.time_stop_mult * self.model["k"]))):
                return mk(price, "TIME_STOP")
            f = self._features(candle)
            if f is not None:
                if np.sign(f["G"]) != self.entry_dir and f["G"] != 0:
                    return mk(price, "INVALIDATION_GAP_FLIP")
                if self.entry_impulse and f["r_l"] * np.sign(self.entry_impulse) < \
                        (1 - self.p.invalidation_frac) * abs(self.entry_impulse):
                    return mk(price, "INVALIDATION_LEADER_RETRACE")
        return None

    def on_tick(self, price: float, timestamp: datetime) -> Optional[StrategySignal]:
        if self.position == 0:
            return None
        if timestamp.time() >= self.p.session_end:
            return StrategySignal(action=SignalAction.EXIT, symbol=self.symbol,
                                  timestamp=timestamp, price=price, reason="TIME_SQUARE_OFF")
        hit_sl = price <= self.stop_loss if self.position == 1 else price >= self.stop_loss
        hit_tp = price >= self.target if self.position == 1 else price <= self.target
        if hit_sl:
            return StrategySignal(action=SignalAction.EXIT, symbol=self.symbol,
                                  timestamp=timestamp, price=self.stop_loss, reason="STOP_LOSS")
        if hit_tp:
            return StrategySignal(action=SignalAction.EXIT, symbol=self.symbol,
                                  timestamp=timestamp, price=self.target, reason="PROFIT_TARGET")
        return None
