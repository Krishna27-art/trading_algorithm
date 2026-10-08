"""
SSF-L5-SRM: Single-Stock Futures lead-lag + Level-5 microprice drift + sector
residual momentum.

Tick/book-driven. The existing candle path cannot carry depth, so:
  - on_book_update(BookSnapshot)  -> entry/cancel/exit logic (primary entry point)
  - on_tick(price, ts)            -> stop / target / 15-min time stop (BaseStrategy contract)
  - on_candle(candle, vwap)       -> updates the regime detector only; never trades
  - replay(snapshots)             -> offline driver over logged live ticks

DATA: needs logged/live mode_full depth for the cash stock + near-month future
(LTP, OI) + a 30-min sector return. None of this exists historically via Kite
REST; it must be recorded live first. The merge of cash + futures streams into
one BookSnapshot is the data layer's job.

PARAMETERS: the source document's composite-score formula, weights, and most
thresholds were lost (symbols stripped). SSFConfig values marked PLACEHOLDER
are NOT from the spec. Values taken from the spec are unmarked.

Order intent: entries are passive LIMITs at the touch. There is no CANCEL
action in SignalAction, so a cancel is emitted as HOLD with reason starting
"CANCEL_ORDER:". Fill confirmation comes from register_trade_entry() as usual.

signal_only mode: when signal_only=True the strategy never creates a pending
order and never simulates a fill.  It emits one BUY/SELL signal per qualifying
direction change, then stays silent until the direction flips.  This is the
correct mode for a live decision-support system.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Deque, Dict, Iterable, List, Optional, Tuple

import numpy as np

from backend.config.settings import InstrumentConfig, StrategyConfig, settings
from backend.monitoring.logger import logger
from backend.strategy.base_strategy import BaseStrategy, SignalAction, StrategySignal

from backend.data.models import BookSnapshot, Level


@dataclass
class SSFConfig:
    # --- from the spec ---
    no_trade_open: Tuple[time, time] = (time(9, 15), time(9, 30))
    no_trade_close: Tuple[time, time] = (time(15, 10), time(15, 30))
    max_spread_ticks: int = 2
    circuit_buffer_pct: float = 1.5
    rest_seconds: float = 2.0
    fill_timeout_seconds: float = 30.0
    cancel_score: float = 1.20
    max_stop_pct: float = 0.40
    reward_risk: float = 2.0
    time_stop_minutes: float = 15.0
    risk_per_trade_pct: float = 0.50     # sizing is done by the existing PositionSizer
    expiry_week_weekdays: Tuple[int, ...] = (1, 2, 3)  # last Tue/Wed/Thu of month
    basis_window_min: int = 30
    # --- PLACEHOLDERS (spec values lost) ---
    level_decay: float = 0.5             # exp(-decay*(n-1)) weight across levels 1..5
    w_mlofi: float = 1.0
    w_micro: float = 1.0
    w_basis: float = 1.0
    w_oi: float = 0.5
    w_sector: float = 0.5
    entry_score: float = 2.0
    basis_z_thr: float = 1.0
    parkinson_pct_range: Tuple[float, float] = (20.0, 90.0)
    fractal_eff_min: float = 0.3
    stop_vol_mult: float = 1.5           # x rolling mid-price std
    z_warmup: int = 60
    z_window: int = 600


class _RollZ:
    def __init__(self, window: int, warmup: int):
        self.buf: Deque[float] = deque(maxlen=window)
        self.warmup = warmup

    def update(self, x: float) -> float:
        z = 0.0
        if len(self.buf) >= self.warmup:
            sd = float(np.std(self.buf))
            if sd > 0:
                z = (x - float(np.mean(self.buf))) / sd
        self.buf.append(x)
        return z


def _in_expiry_week(d: date, weekdays: Tuple[int, ...]) -> bool:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return d.weekday() in weekdays and (last - d).days < 7


def select_candidates(basis_z: Dict[str, float], sector_resid_var: Dict[str, float],
                      universe_top: int = 50, pick: int = 5) -> List[str]:
    """Stock selection engine: rank by sector-residual variance, keep top
    `universe_top`, then pick `pick` with largest |basis z|."""
    ranked = sorted(sector_resid_var, key=sector_resid_var.get, reverse=True)[:universe_top]
    return sorted(ranked, key=lambda s: abs(basis_z.get(s, 0.0)), reverse=True)[:pick]


class SsfL5SrmStrategy(BaseStrategy):
    def __init__(
        self,
        instrument: InstrumentConfig,
        strategy_config: StrategyConfig = settings.strategy,
        ssf: Optional[SSFConfig] = None,
        signal_only: bool = False,
    ):
        super().__init__(instrument.symbol)
        self.instrument = instrument
        self.config = strategy_config
        self.p = ssf or SSFConfig()
        self.tick = instrument.tick_size or 0.05

        z = lambda: _RollZ(self.p.z_window, self.p.z_warmup)
        self._z_mlofi, self._z_micro, self._z_oi, self._z_sector = z(), z(), z(), z()
        self._basis_hist: Deque[Tuple[datetime, float]] = deque()
        self._mids: Deque[float] = deque(maxlen=self.p.z_window)
        self._prev: Optional[BookSnapshot] = None
        self._prev_oi: Optional[Tuple[datetime, float]] = None

        # regime state (fed by on_candle)
        self._bars: Deque[dict] = deque(maxlen=400)
        self._parkinson_hist: Deque[float] = deque(maxlen=400)
        self.regime_ok: bool = False

        # order / position state
        self.pending: Optional[dict] = None   # {dir, price, t0}
        self.entry_time: Optional[datetime] = None
        self.last_features: Dict[str, float] = {}

        # signal-only mode: emit one signal per direction change, no order management
        self.signal_only: bool = bool(signal_only)
        self._last_signal_direction: int = 0

    # ------------------------------------------------------------- lifecycle
    def reset_session(self, session_date: date):
        self.current_date = session_date
        self.position = 0
        self.entry_price = self.stop_loss = self.target = self.initial_risk_dist = 0.0
        self.trailing_breakeven_active = False
        self.trades_today = 0
        self.pending = None
        self.entry_time = None
        self._prev = None
        self._prev_oi = None
        self._basis_hist.clear()
        self._mids.clear()
        self._last_signal_direction = 0
        self.last_features = {}
        # NOTE: _bars and _parkinson_hist are intentionally NOT cleared here.
        # They hold the 1-minute regime history which must survive across sessions.

    def register_trade_entry(self, entry_price, position, stop_loss, target, risk_dist):
        super().register_trade_entry(entry_price, position, stop_loss, target, risk_dist)
        self.pending = None
        self.entry_time = self._prev.timestamp if self._prev else None

    def register_trade_exit(self):
        super().register_trade_exit()
        self.entry_time = None

    # ---------------------------------------------------------------- regime
    def on_candle(self, candle: dict, vwap: Optional[float] = None) -> Optional[StrategySignal]:
        """Regime detector only. Parkinson-vol percentile + fractal efficiency
        over the last 30 bars (assumes 1-min candles)."""
        self._bars.append(candle)
        n = 30
        if len(self._bars) < n:
            self.regime_ok = False
            return None
        w = list(self._bars)[-n:]
        pk = math.sqrt(sum(math.log(b["high"] / b["low"]) ** 2 for b in w if b["low"] > 0) / (4 * n * math.log(2)))
        self._parkinson_hist.append(pk)
        closes = [b["close"] for b in w]
        path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, n))
        er = abs(closes[-1] - closes[0]) / path if path > 0 else 0.0
        pct = 100.0 * sum(1 for x in self._parkinson_hist if x <= pk) / len(self._parkinson_hist)
        lo, hi = self.p.parkinson_pct_range
        self.regime_ok = (lo <= pct <= hi) and er >= self.p.fractal_eff_min
        return None

    # -------------------------------------------------------------- features
    @staticmethod
    def _ofi_level(b, a, pb, pa) -> float:
        (bp, bq, _), (ap, aq, _), (pbp, pbq, _), (pap, paq, _) = b, a, pb, pa
        return ((bq if bp >= pbp else 0) - (pbq if bp <= pbp else 0)
                - (aq if ap <= pap else 0) + (paq if ap >= pap else 0))

    def _features(self, s: BookSnapshot) -> Optional[Dict[str, float]]:
        if len(s.bids) < 5 or len(s.asks) < 5:
            return None
        bb, ba = s.bids[0], s.asks[0]
        mid = 0.5 * (bb[0] + ba[0])
        self._mids.append(mid)

        mlofi = 0.0
        if self._prev is not None and len(self._prev.bids) >= 5:
            for n in range(5):
                w = math.exp(-self.p.level_decay * n)
                mlofi += w * self._ofi_level(s.bids[n], s.asks[n], self._prev.bids[n], self._prev.asks[n])
        z_mlofi = self._z_mlofi.update(mlofi)

        qb, qa = bb[1], ba[1]
        micro = (bb[0] * qa + ba[0] * qb) / (qa + qb) if (qa + qb) > 0 else mid
        z_micro = self._z_micro.update(micro - mid)

        z_basis = 0.0
        if s.fut_ltp is not None and s.ltp > 0:
            self._basis_hist.append((s.timestamp, (s.fut_ltp - s.ltp) / s.ltp))
            cutoff = s.timestamp - timedelta(minutes=self.p.basis_window_min)
            while self._basis_hist and self._basis_hist[0][0] < cutoff:
                self._basis_hist.popleft()
            vals = [b for _, b in self._basis_hist]
            if len(vals) >= self.p.z_warmup and np.std(vals) > 0:
                z_basis = (vals[-1] - np.mean(vals)) / np.std(vals)

        z_oi = 0.0
        if s.fut_oi is not None:
            if self._prev_oi is not None:
                dt = max((s.timestamp - self._prev_oi[0]).total_seconds(), 1e-6)
                z_oi = self._z_oi.update((s.fut_oi - self._prev_oi[1]) / dt)
            self._prev_oi = (s.timestamp, s.fut_oi)

        z_sec = 0.0
        if s.sector_ret_30m is not None and s.stock_ret_30m is not None:
            z_sec = self._z_sector.update(s.stock_ret_30m - s.sector_ret_30m)

        p = self.p
        score = (p.w_mlofi * z_mlofi + p.w_micro * z_micro + p.w_basis * z_basis
                 + p.w_oi * z_oi + p.w_sector * z_sec)
        return dict(mid=mid, z_mlofi=z_mlofi, z_micro=z_micro, z_basis=z_basis,
                    z_oi=z_oi, z_sector=z_sec, score=score, micro_dev=micro - mid,
                    spread_ticks=(ba[0] - bb[0]) / self.tick)

    def _has_required_context(
        self,
        s: BookSnapshot,
        max_age_seconds: int = 120,
    ) -> bool:
        """
        Return True only when all required SSF external data exists
        and each source timestamp is fresh relative to the cash-book
        snapshot timestamp.
        """
        required_values = (
            s.fut_ltp,
            s.fut_oi,
            s.sector_ret_30m,
            s.stock_ret_30m,
        )

        if any(
            value is None
            for value in required_values
        ):
            return False

        required_timestamps = (
            s.futures_updated_at,
            s.sector_return_updated_at,
            s.stock_return_updated_at,
        )

        if any(
            ts is None
            for ts in required_timestamps
        ):
            return False

        reference_time = s.timestamp

        for updated_at in required_timestamps:
            age_seconds = (
                reference_time - updated_at
            ).total_seconds()

            if (
                age_seconds < 0
                or age_seconds > max_age_seconds
            ):
                return False

        return True

    def _blocked(self, s: BookSnapshot, f: Dict[str, float]) -> Optional[str]:
        t = s.timestamp.time()
        for a, b in (self.p.no_trade_open, self.p.no_trade_close):
            if a <= t < b:
                return "no_trade_window"
        if _in_expiry_week(s.timestamp.date(), self.p.expiry_week_weekdays):
            return "expiry_week"
        if f["spread_ticks"] > self.p.max_spread_ticks:
            return "wide_spread"
        for lim in (s.circuit_lower, s.circuit_upper):
            if lim and abs(s.ltp - lim) / lim * 100 <= self.p.circuit_buffer_pct:
                return "near_circuit"
        if not self.regime_ok:
            return "regime"
        return None

    # --------------------------------------------------------------- signals
    def on_book_update(self, s: BookSnapshot) -> Optional[StrategySignal]:
        f = self._features(s)
        self._prev = s
        if f is None:
            return None
        self.last_features = f

        # In signal-only mode, require real external context and active regime.
        if self.signal_only:
            if not self._has_required_context(s):
                self._last_signal_direction = 0
                return None
            if not self.regime_ok:
                self._last_signal_direction = 0
                return None

        sig = lambda a, px, why, **kw: StrategySignal(action=a, symbol=self.symbol,
                                                      timestamp=s.timestamp, price=px, reason=why, **kw)
        # --- pending passive order management (backtest/execution mode only)
        if not self.signal_only and self.pending is not None:
            age = (s.timestamp - self.pending["t0"]).total_seconds()
            d, px = self.pending["dir"], self.pending["price"]
            chased = (d == 1 and s.bids[0][0] > px) or (d == -1 and s.asks[0][0] < px)
            if age >= self.p.fill_timeout_seconds or chased or f["score"] * d < self.p.cancel_score:
                why = "timeout" if age >= self.p.fill_timeout_seconds else ("price_moved" if chased else "score_decay")
                self.pending = None
                return sig(SignalAction.HOLD, px, f"CANCEL_ORDER:{why}")
            return None

        # --- open position: microstructure / basis exits (stop/target/time in on_tick)
        if not self.signal_only and self.position != 0:
            if f["micro_dev"] * self.position < 0:
                return sig(SignalAction.EXIT, s.ltp, "MICROPRICE_INVERSION")
            if f["z_basis"] * self.position <= 0:
                return sig(SignalAction.EXIT, s.ltp, "BASIS_RECONVERGENCE")
            return self.on_tick(s.ltp, s.timestamp)

        # --- entry
        if self.trades_today >= self.config.max_trades_per_instrument_day or self._blocked(s, f):
            return None
        p = self.p
        d = 0
        if f["score"] >= p.entry_score and f["z_basis"] >= p.basis_z_thr and f["z_oi"] > 0 and f["z_sector"] > 0:
            d = 1
        elif f["score"] <= -p.entry_score and f["z_basis"] <= -p.basis_z_thr and f["z_oi"] > 0 and f["z_sector"] < 0:
            d = -1   # OI expansion on short positioning: z_oi > 0 as well

        if d == 0:
            if self.signal_only:
                self._last_signal_direction = 0
            return None

        px = s.bids[0][0] if d == 1 else s.asks[0][0]
        vol = float(np.std(self._mids)) if len(self._mids) > 10 else 0.0
        risk = min(p.stop_vol_mult * vol, p.max_stop_pct / 100.0 * px)
        if risk < self.tick:
            return None

        if self.signal_only:
            # Emit one signal when the qualifying direction appears.
            # Do not pretend that a manual order was filled.
            if self._last_signal_direction == d:
                return None
            self._last_signal_direction = d
        else:
            self.pending = dict(dir=d, price=px, t0=s.timestamp)

        return sig(
            SignalAction.BUY if d == 1 else SignalAction.SELL,
            px,
            (
                f"SSF-L5-SRM "
                f"score={f['score']:.2f} "
                f"basis_z={f['z_basis']:.2f} "
                f"micro_dev={f['micro_dev']:.3f}"
            ),
            stop_loss=px - d * risk,
            target=px + d * p.reward_risk * risk,
            order_type="LIMIT",
        )

    def on_tick(self, price: float, timestamp: datetime) -> Optional[StrategySignal]:
        if self.position == 0:
            return None
        mk = lambda px, why: StrategySignal(action=SignalAction.EXIT, symbol=self.symbol,
                                            timestamp=timestamp, price=px, reason=why)
        if timestamp.time() >= self.p.no_trade_close[0]:
            return mk(price, "TIME_SQUARE_OFF")
        if self.entry_time and (timestamp - self.entry_time).total_seconds() >= self.p.time_stop_minutes * 60:
            return mk(price, "TIME_STOP_15M")
        sl_hit = price <= self.stop_loss if self.position == 1 else price >= self.stop_loss
        tp_hit = price >= self.target if self.position == 1 else price <= self.target
        if sl_hit:
            return mk(self.stop_loss, "STOP_LOSS")
        if tp_hit:
            return mk(self.target, "PROFIT_TARGET")
        return None

    # ------------------------------------------------------------ offline use
    def replay(self, snapshots: Iterable[BookSnapshot]) -> List[StrategySignal]:
        """Drive the strategy over logged snapshots. Fills are NOT simulated
        here (spec: 40% passive fill at touch) — that belongs to the execution
        simulator; this returns the raw signal stream."""
        out = []
        for s in snapshots:
            sg = self.on_book_update(s)
            if sg:
                out.append(sg)
        return out
