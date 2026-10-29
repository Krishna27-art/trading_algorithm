"""
Monte Carlo robustness check for a list of COMPLETED backtest trades.

It answers: "if these same trades had arrived in a different order, or the
future draws a similar-but-different mix of them, how bad can it get?"

Two resampling modes over the real per-trade net P&L (nothing synthetic is
added; only the order/mix of real outcomes changes):

  * shuffle   - permutes the order of the exact trade list. Final P&L is
                unchanged; it stresses drawdown and losing-streak length.
  * bootstrap - draws trades with replacement. Final P&L varies; it gives a
                spread of plausible outcomes and a probability of loss.

A small sample cannot prove an edge, so results below MIN_TRADES are
returned with ``reliable = False``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence

import numpy as np

MIN_TRADES = 30


@dataclass
class MonteCarloResult:
    trades: int
    simulations: int
    reliable: bool
    note: str
    # shuffle (order risk)
    max_drawdown_pct_p50: Optional[float]
    max_drawdown_pct_p95: Optional[float]
    max_drawdown_pct_worst: Optional[float]
    losing_streak_p95: Optional[int]
    # bootstrap (outcome spread)
    net_pnl_p05: Optional[float]
    net_pnl_p50: Optional[float]
    net_pnl_p95: Optional[float]
    probability_of_loss_pct: Optional[float]
    probability_of_ruin_pct: Optional[float]
    ruin_drawdown_pct: float

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def _pnls(trades: Sequence[dict]) -> np.ndarray:
    values = []
    for t in trades:
        v = t.get("pnl_net", t.get("net_pnl"))
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if np.isfinite(f):
            values.append(f)
    return np.asarray(values, dtype=float)


def _max_drawdown_pct(paths: np.ndarray, initial_capital: float) -> np.ndarray:
    equity = initial_capital + np.cumsum(paths, axis=1)
    equity = np.concatenate([np.full((paths.shape[0], 1), initial_capital), equity], axis=1)
    peak = np.maximum.accumulate(equity, axis=1)
    dd = (peak - equity) / peak
    return dd.max(axis=1) * 100.0


def _max_losing_streak(row: np.ndarray) -> int:
    best = cur = 0
    for v in row:
        if v <= 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def run_monte_carlo(
    trades: Sequence[dict],
    initial_capital: float = 1_000_000.0,
    simulations: int = 5000,
    seed: int = 7,
    ruin_drawdown_pct: float = 30.0,
) -> MonteCarloResult:
    pnl = _pnls(trades)
    n = int(pnl.size)
    if n == 0:
        return MonteCarloResult(
            trades=0, simulations=0, reliable=False, note="No completed trades to simulate.",
            max_drawdown_pct_p50=None, max_drawdown_pct_p95=None, max_drawdown_pct_worst=None,
            losing_streak_p95=None, net_pnl_p05=None, net_pnl_p50=None, net_pnl_p95=None,
            probability_of_loss_pct=None, probability_of_ruin_pct=None,
            ruin_drawdown_pct=ruin_drawdown_pct,
        )

    rng = np.random.default_rng(seed)
    sims = int(max(100, simulations))

    shuffled = np.stack([rng.permutation(pnl) for _ in range(sims)])
    dd_shuffle = _max_drawdown_pct(shuffled, initial_capital)
    streaks = np.array([_max_losing_streak(r) for r in shuffled])

    boot = rng.choice(pnl, size=(sims, n), replace=True)
    finals = boot.sum(axis=1)
    dd_boot = _max_drawdown_pct(boot, initial_capital)

    reliable = n >= MIN_TRADES
    note = (
        "Resampled from real trade outcomes."
        if reliable
        else f"Only {n} trades; at least {MIN_TRADES} are needed before these numbers mean much."
    )
    return MonteCarloResult(
        trades=n,
        simulations=sims,
        reliable=reliable,
        note=note,
        max_drawdown_pct_p50=round(float(np.percentile(dd_shuffle, 50)), 2),
        max_drawdown_pct_p95=round(float(np.percentile(dd_shuffle, 95)), 2),
        max_drawdown_pct_worst=round(float(dd_shuffle.max()), 2),
        losing_streak_p95=int(np.percentile(streaks, 95)),
        net_pnl_p05=round(float(np.percentile(finals, 5)), 2),
        net_pnl_p50=round(float(np.percentile(finals, 50)), 2),
        net_pnl_p95=round(float(np.percentile(finals, 95)), 2),
        probability_of_loss_pct=round(float((finals <= 0).mean() * 100.0), 1),
        probability_of_ruin_pct=round(float((dd_boot >= ruin_drawdown_pct).mean() * 100.0), 1),
        ruin_drawdown_pct=ruin_drawdown_pct,
    )
