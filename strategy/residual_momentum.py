"""
Cross-Sectional Residual Momentum with Dynamic Volatility Scaling.

STRATEGY CODE : NSE-RM-100-PROD
SIGNAL ID     : NSE_ALGO_RES_MOM_V1
UNIVERSE      : NIFTY 100 constituents (point-in-time), ADTV20 >= Rs 50 Cr
CADENCE       : every alternate Friday, 15:00-15:10 IST, EOD data only
PRODUCT       : CNC delivery long legs + short NIFTY index futures macro hedge

Mechanism
---------
For each stock a rolling 252-day OLS of excess return on NIFTY 50 excess
return is fitted on the window ENDING AT t-1. The fitted alpha/beta strip
market exposure out of the daily returns; the residuals are summed over the
126 sessions ending 10 sessions ago (the 10-day lag skips short-term bid-ask
reversal), scaled by residual volatility, then z-scored across the
cross-section. Top-decile names that also clear liquidity, trend and circuit
filters get inverse-volatility weights.

What this deliberately does NOT do
----------------------------------
  * No short cash equity legs. SEBI does not allow unhedged overnight cash
    shorts; downside is handled by selling NIFTY index futures (cash-settled,
    so no physical-delivery exposure) sized to portfolio beta.
  * No single-stock futures anywhere, for the same physical-settlement reason.
  * No intraday data. Everything runs off the daily bhavcopy / Kite daily
    candles, which is what keeps turnover at 6-12x p.a. and STT drag near
    -2% p.a. rather than the -12%+ an intraday engine pays.

Look-ahead discipline
---------------------
Every frame passed in must be sliced by the caller so its LAST row is
session t-1. The engine never reads a row dated t. Day t is used by the
execution layer only, to place the orders this plan describes. There is a
test for this in tests/test_portfolio_strategies.py.

Survivorship
------------
`universe` is a per-date argument, not a module constant. Backtests must
pass the constituent list as it stood on that historical date, otherwise
returns are inflated by names that were never in the index at the time
(Yes Bank, DHFL, Zee, RCom).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from config.settings import ResidualMomentumConfig, settings
from strategy.portfolio_base import (
    ExitSignal,
    HedgeOrder,
    MarketRegime,
    OrderSide,
    PortfolioStrategy,
    RebalanceOrder,
    RebalancePlan,
    TargetPosition,
)

try:  # keeps the module importable in a bare unit-test environment
    from monitoring.logger import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("residual_momentum")


CRORE = 1e7


class ResidualMomentumStrategy(PortfolioStrategy):
    name = "Cross-Sectional Residual Momentum with Dynamic Volatility Scaling"
    strategy_code = "NSE-RM-100-PROD"
    signal_id = "NSE_ALGO_RES_MOM_V1"

    def __init__(
        self,
        config: Optional[ResidualMomentumConfig] = None,
        sector_map: Optional[Dict[str, str]] = None,
        anchor_date: Optional[date] = None,
    ):
        if config is not None:
            self.config = config
        else:
            try:
                from config.settings import settings
                self.config = settings.rm100
            except Exception:
                self.config = ResidualMomentumConfig()
        self.sector_map = sector_map or {}
        # Fortnightly parity is anchored to an explicit date so that a restart
        # mid-backtest lands on the same Fridays as the original run.
        self.anchor_date = anchor_date or date(2018, 1, 5)
        self.high_water_equity: float = 0.0

    # ------------------------------------------------------------------ #
    # Schedule
    # ------------------------------------------------------------------ #
    def is_rebalance_day(self, session_date: date) -> bool:
        if session_date.weekday() != self.config.rebalance_weekday:
            return False
        weeks = (session_date - self.anchor_date).days // 7
        return weeks % self.config.rebalance_parity_weeks == 0

    # ------------------------------------------------------------------ #
    # Core maths
    # ------------------------------------------------------------------ #
    @staticmethod
    def daily_risk_free(annual_yield_pct: float) -> float:
        """91-day T-bill yield (percent p.a.) -> daily compounded rate."""
        return (1.0 + annual_yield_pct / 100.0) ** (1.0 / 252.0) - 1.0

    @staticmethod
    def _ols_alpha_beta(y: np.ndarray, x: np.ndarray) -> Tuple[float, float]:
        """Closed-form single-factor OLS. Returns (alpha, beta)."""
        x_bar = x.mean()
        y_bar = y.mean()
        denom = float(((x - x_bar) ** 2).sum())
        if denom <= 0.0 or not np.isfinite(denom):
            return 0.0, 0.0
        beta = float(((x - x_bar) * (y - y_bar)).sum() / denom)
        alpha = float(y_bar - beta * x_bar)
        return alpha, beta

    def compute_scores(
        self,
        closes: pd.DataFrame,
        index_close: pd.Series,
        rf_daily: pd.Series,
        universe: Sequence[str],
    ) -> pd.DataFrame:
        """
        closes       : wide frame, index=session date (ASC, last row = t-1),
                       columns=symbol, values=split/dividend-ADJUSTED close.
        index_close  : NIFTY 50 close on the same index.
        rf_daily     : daily risk-free rate on the same index.
        universe     : point-in-time NIFTY 100 membership on the rebalance date.

        Returns one row per symbol with beta, cum residual, residual vol,
        raw score and the cross-sectional z-score.
        """
        cfg = self.config
        need = cfg.regression_window + 1
        if len(closes) < need:
            raise ValueError(
                f"Need at least {need} sessions of history, got {len(closes)}"
            )

        idx_ret = index_close.pct_change()
        rf = rf_daily.reindex(idx_ret.index).ffill().fillna(0.0)
        idx_excess = (idx_ret - rf).iloc[-cfg.regression_window:]

        # Residual slice: the K sessions ending L sessions before t.
        # With K=126, L=10 this sits wholly inside the 252-day fit window.
        start = -(cfg.momentum_window)
        stop = -(cfg.lag_buffer) if cfg.lag_buffer > 0 else None

        rows = []
        for symbol in universe:
            if symbol not in closes.columns:
                continue
            px = closes[symbol]
            ret = px.pct_change()
            excess = (ret - rf).iloc[-cfg.regression_window:]

            pair = pd.concat([excess, idx_excess], axis=1).dropna()
            if len(pair) < cfg.regression_window * 0.8:
                continue  # too many gaps (suspension, late listing) to trust

            y = pair.iloc[:, 0].to_numpy(dtype=float)
            x = pair.iloc[:, 1].to_numpy(dtype=float)
            alpha, beta = self._ols_alpha_beta(y, x)

            resid_full = pd.Series(y - (alpha + beta * x), index=pair.index)
            resid = resid_full.iloc[start:stop]
            if len(resid) < 2:
                continue

            cum_resid = float(resid.sum())
            resid_vol = float(resid.std(ddof=1))
            if not np.isfinite(resid_vol) or resid_vol <= 0.0:
                continue

            rows.append(
                {
                    "symbol": symbol,
                    "alpha": alpha,
                    "beta": beta,
                    "cum_residual": cum_resid,
                    "residual_vol": resid_vol,
                    "rm_raw": cum_resid / resid_vol,
                }
            )

        scores = pd.DataFrame(rows)
        if scores.empty:
            return scores

        mu = scores["rm_raw"].mean()
        sd = scores["rm_raw"].std(ddof=1)
        scores["rm_z"] = 0.0 if (not np.isfinite(sd) or sd == 0) else (scores["rm_raw"] - mu) / sd
        scores["rm_z"] = scores["rm_z"].clip(-3.0, 3.0)
        return scores.sort_values("rm_z", ascending=False).reset_index(drop=True)

    # ------------------------------------------------------------------ #
    # Filters
    # ------------------------------------------------------------------ #
    def _liquidity_and_trend_mask(
        self,
        closes: pd.DataFrame,
        volumes: pd.DataFrame,
        symbols: Sequence[str],
        circuit_hit: Optional[Sequence[str]] = None,
    ) -> Dict[str, bool]:
        cfg = self.config
        blocked = set(circuit_hit or [])
        out: Dict[str, bool] = {}
        for s in symbols:
            if s in blocked or s not in closes.columns or s not in volumes.columns:
                out[s] = False
                continue
            traded_value = (closes[s] * volumes[s]).iloc[-cfg.adtv_window:]
            adtv = float(traded_value.mean())
            sma50 = float(closes[s].iloc[-cfg.trend_filter_sma:].mean())
            last = float(closes[s].iloc[-1])
            out[s] = bool(
                np.isfinite(adtv)
                and adtv >= cfg.min_adtv_rupees
                and np.isfinite(last)
                and last > sma50
            )
        return out

    def detect_regime(self, index_close: pd.Series) -> MarketRegime:
        cfg = self.config
        sma200 = float(index_close.iloc[-cfg.regime_sma:].mean())
        return MarketRegime.BULLISH if float(index_close.iloc[-1]) > sma200 else MarketRegime.DEFENSIVE

    # ------------------------------------------------------------------ #
    # Sizing
    # ------------------------------------------------------------------ #
    def _inverse_vol_weights(
        self, closes: pd.DataFrame, symbols: Sequence[str]
    ) -> Dict[str, float]:
        cfg = self.config
        raw: Dict[str, float] = {}
        for s in symbols:
            vol = float(closes[s].pct_change().iloc[-cfg.vol_window:].std(ddof=1))
            raw[s] = 1.0 / vol if np.isfinite(vol) and vol > 0 else 0.0
        total = sum(raw.values())
        if total <= 0:
            n = max(len(symbols), 1)
            return {s: 1.0 / n for s in symbols}
        return {s: v / total for s, v in raw.items()}

    def _apply_caps(self, weights: Dict[str, float]) -> Dict[str, float]:
        """Cap at 15%, floor at 4%, redistribute the spill proportionally
        among names that are still inside their band. Bounded iteration --
        with J=10 and a 15% cap the constraint set is always feasible."""
        cfg = self.config
        w = dict(weights)
        n = max(len(w), 1)
        # With fewer names than 1/max_weight the cap is arithmetically
        # infeasible (4 names cannot each stay under 15% and still sum to 1).
        # Widen the band to the nearest feasible one rather than silently
        # emitting weights that breach the stated limit.
        cap = max(cfg.max_weight, 1.0 / n)
        floor = min(cfg.min_weight, 1.0 / n)
        for _ in range(25):
            over = {s: v for s, v in w.items() if v > cap + 1e-12}
            under = {s: v for s, v in w.items() if v < floor - 1e-12}
            if not over and not under:
                break
            for s in over:
                w[s] = cap
            for s in under:
                w[s] = floor
            fixed = set(over) | set(under)
            free = [s for s in w if s not in fixed]
            spill = 1.0 - sum(w.values())
            if not free or abs(spill) < 1e-12:
                break
            free_total = sum(w[s] for s in free)
            if free_total <= 0:
                for s in free:
                    w[s] += spill / len(free)
            else:
                for s in free:
                    w[s] += spill * (w[s] / free_total)
        total = sum(w.values())
        return {s: v / total for s, v in w.items()} if total > 0 else w

    def _apply_sector_cap(self, weights: Dict[str, float]) -> Dict[str, float]:
        cfg = self.config
        if not self.sector_map:
            return weights
        w = dict(weights)
        for _ in range(10):
            by_sector: Dict[str, List[str]] = {}
            for s in w:
                by_sector.setdefault(self.sector_map.get(s, "UNKNOWN"), []).append(s)
            breach = {
                sec: names
                for sec, names in by_sector.items()
                if sec != "UNKNOWN" and sum(w[n] for n in names) > cfg.max_sector_weight + 1e-12
            }
            if not breach:
                break
            for sec, names in breach.items():
                current = sum(w[n] for n in names)
                scale = cfg.max_sector_weight / current
                for n in names:
                    w[n] *= scale
            total = sum(w.values())
            others = [s for s in w if self.sector_map.get(s, "UNKNOWN") not in breach]
            spill = 1.0 - total
            if others and abs(spill) > 1e-12:
                other_total = sum(w[s] for s in others)
                for s in others:
                    w[s] += spill * (w[s] / other_total if other_total > 0 else 1.0 / len(others))
            else:
                break
        total = sum(w.values())
        return {s: v / total for s, v in w.items()} if total > 0 else w

    # ------------------------------------------------------------------ #
    # Plan generation
    # ------------------------------------------------------------------ #
    def generate_plan(
        self,
        as_of: date,
        closes: pd.DataFrame,
        highs: pd.DataFrame,
        lows: pd.DataFrame,
        volumes: pd.DataFrame,
        index_close: pd.Series,
        universe: Sequence[str],
        capital: float,
        rf_yield_pct: float = 6.5,
        current_holdings: Optional[Dict[str, int]] = None,
        current_equity: Optional[float] = None,
        circuit_hit: Optional[Sequence[str]] = None,
        corporate_action_symbols: Optional[Sequence[str]] = None,
        blackout: bool = False,
        index_futures_symbol: str = "NIFTY-FUT",
        index_futures_price: Optional[float] = None,
    ) -> RebalancePlan:
        """
        All frames must END at session t-1 (see module docstring).
        `capital` is the capital allocated to THIS strategy, not the whole book.
        `blackout` -> True on Union Budget day and general election result day.
        """
        cfg = self.config
        holdings = dict(current_holdings or {})
        ca_block = set(corporate_action_symbols or [])

        def _skip(reason: str) -> RebalancePlan:
            logger.warning(f"[{self.strategy_code}] rebalance skipped {as_of}: {reason}")
            return RebalancePlan(
                as_of=as_of,
                regime=self.detect_regime(index_close),
                gross_exposure=0.0,
                targets=[],
                orders=[],
                skipped=True,
                skip_reason=reason,
            )

        if blackout:
            return _skip("BLACKOUT_BUDGET_OR_ELECTION_RESULT")

        regime = self.detect_regime(index_close)
        gross = (
            cfg.bullish_gross_exposure
            if regime == MarketRegime.BULLISH
            else cfg.defensive_gross_exposure
        )

        # Drawdown gate: equity below the high-water mark by more than 12%
        # halves exposure until a new 30-day high is printed upstream.
        equity = current_equity if current_equity is not None else capital
        self.high_water_equity = max(self.high_water_equity, equity)
        drawdown = 0.0
        if self.high_water_equity > 0:
            drawdown = (self.high_water_equity - equity) / self.high_water_equity
        if drawdown > cfg.max_drawdown_gate:
            gross *= cfg.drawdown_exposure_cut

        scores = self.compute_scores(closes, index_close, self._rf_series(closes.index, rf_yield_pct), universe)
        if scores.empty:
            return _skip("NO_SCOREABLE_CONSTITUENTS")

        eligible = self._liquidity_and_trend_mask(closes, volumes, list(scores["symbol"]), circuit_hit)
        valid_count = sum(1 for s in scores["symbol"] if s in closes.columns)
        if valid_count < cfg.min_valid_universe:
            return _skip(f"UNIVERSE_TOO_THIN ({valid_count} < {cfg.min_valid_universe})")

        decile_cut = scores["rm_z"].quantile(1.0 - cfg.top_decile_pct)
        candidates = scores[
            (scores["rm_z"] >= decile_cut)
            & scores["symbol"].map(lambda s: eligible.get(s, False))
            & ~scores["symbol"].isin(ca_block)
        ].head(cfg.portfolio_size)

        if candidates.empty:
            return _skip("NO_CANDIDATES_PASSED_FILTERS")

        selected = list(candidates["symbol"])
        weights = self._apply_sector_cap(self._apply_caps(self._inverse_vol_weights(closes, selected)))

        atr = self._atr(highs, lows, closes, selected)

        targets: List[TargetPosition] = []
        for rank, (_, row) in enumerate(candidates.iterrows(), start=1):
            s = row["symbol"]
            ref = float(closes[s].iloc[-1])
            w = weights.get(s, 0.0) * gross
            qty = int((capital * w) // ref) if ref > 0 else 0
            stop = ref - cfg.stop_atr_multiple * atr.get(s, 0.0)
            targets.append(
                TargetPosition(
                    symbol=s,
                    weight=w,
                    score=float(row["rm_z"]),
                    rank=rank,
                    reference_price=ref,
                    quantity=qty,
                    stop_loss=round(stop, 2) if atr.get(s) else None,
                    sector=self.sector_map.get(s),
                )
            )

        orders = self._diff_to_orders(targets, holdings, closes, scores)

        hedge = None
        if regime == MarketRegime.DEFENSIVE and targets:
            hedge = self._build_hedge(
                targets=targets,
                scores=scores,
                capital=capital,
                index_price=index_futures_price or float(index_close.iloc[-1]),
                symbol=index_futures_symbol,
            )

        return RebalancePlan(
            as_of=as_of,
            regime=regime,
            gross_exposure=gross,
            targets=targets,
            orders=orders,
            hedge=hedge,
            diagnostics={
                "scored_universe": int(len(scores)),
                "decile_cut_z": float(decile_cut),
                "drawdown_from_hwm": round(drawdown, 4),
                "signal_id": self.signal_id,
            },
        )

    def _rf_series(self, index: pd.Index, annual_yield_pct: float) -> pd.Series:
        return pd.Series(self.daily_risk_free(annual_yield_pct), index=index)

    def _diff_to_orders(
        self,
        targets: List[TargetPosition],
        holdings: Dict[str, int],
        closes: pd.DataFrame,
        scores: pd.DataFrame,
    ) -> List[RebalanceOrder]:
        """Target book minus current book. Holdings that drop out of the top
        decile are cut only if their rescored z also sits below P75 -- that
        stops a name oscillating around rank 10 from being churned every
        fortnight and paying STT for nothing."""
        cfg = self.config
        target_qty = {t.symbol: t.quantity for t in targets}
        ref_price = {t.symbol: t.reference_price for t in targets}
        z_by_symbol = dict(zip(scores["symbol"], scores["rm_z"]))
        exit_cut = scores["rm_z"].quantile(cfg.exit_percentile)

        orders: List[RebalanceOrder] = []

        for symbol, held in holdings.items():
            if held <= 0:
                continue
            if symbol in target_qty:
                continue
            z = z_by_symbol.get(symbol)
            if z is None or z < exit_cut:
                price = float(closes[symbol].iloc[-1]) if symbol in closes.columns else 0.0
                orders.append(
                    RebalanceOrder(
                        symbol=symbol,
                        side=OrderSide.SELL,
                        quantity=held,
                        reference_price=price,
                        reason="RECONSTITUTION_BELOW_P75",
                    )
                )

        for t in targets:
            held = holdings.get(t.symbol, 0)
            delta = t.quantity - held
            if delta == 0:
                continue
            orders.append(
                RebalanceOrder(
                    symbol=t.symbol,
                    side=OrderSide.BUY if delta > 0 else OrderSide.SELL,
                    quantity=abs(delta),
                    reference_price=ref_price[t.symbol],
                    reason="REBALANCE_TOP_DECILE" if delta > 0 else "REBALANCE_TRIM",
                )
            )

        # Cap at the spec's 10 orders per cycle, exits first (they free capital).
        exits = [o for o in orders if o.reason.startswith("RECONSTITUTION")]
        rest = [o for o in orders if not o.reason.startswith("RECONSTITUTION")]
        return (exits + rest)[: max(cfg.portfolio_size, len(exits))]

    def _build_hedge(
        self,
        targets: List[TargetPosition],
        scores: pd.DataFrame,
        capital: float,
        index_price: float,
        symbol: str,
    ) -> Optional[HedgeOrder]:
        cfg = self.config
        beta_by_symbol = dict(zip(scores["symbol"], scores["beta"]))
        port_beta = sum(t.weight * beta_by_symbol.get(t.symbol, 1.0) for t in targets)
        notional = capital * cfg.defensive_gross_exposure * port_beta
        if index_price <= 0:
            return None
        lots = int(round(notional / (index_price * cfg.nifty_lot_size)))
        if lots <= 0:
            return None
        return HedgeOrder(
            symbol=symbol,
            side=OrderSide.SELL,
            lots=lots,
            lot_size=cfg.nifty_lot_size,
            reference_price=index_price,
            portfolio_beta=round(port_beta, 4),
            notional=round(notional, 2),
        )

    # ------------------------------------------------------------------ #
    # Between-rebalance monitoring
    # ------------------------------------------------------------------ #
    def _atr(
        self,
        highs: pd.DataFrame,
        lows: pd.DataFrame,
        closes: pd.DataFrame,
        symbols: Sequence[str],
    ) -> Dict[str, float]:
        n = self.config.atr_window
        out: Dict[str, float] = {}
        for s in symbols:
            if s not in highs.columns or s not in lows.columns:
                continue
            h, l, c = highs[s], lows[s], closes[s]
            prev_close = c.shift(1)
            tr = pd.concat(
                [h - l, (h - prev_close).abs(), (l - prev_close).abs()], axis=1
            ).max(axis=1)
            val = float(tr.iloc[-n:].mean())
            if np.isfinite(val):
                out[s] = val
        return out

    def monitor(
        self,
        session_date: date,
        holdings: Dict[str, dict],
        closes: pd.DataFrame,
        intraday_lows: Optional[Dict[str, float]] = None,
        timestamp: Optional[datetime] = None,
    ) -> List[ExitSignal]:
        """
        holdings: {symbol: {"quantity": int, "entry_price": float,
                            "stop_loss": float, "trail_armed": bool}}
        intraday_lows: today's running low per symbol, for the hard ATR stop.
                       Omit it to run purely on the EOD close.

        Emits, per symbol:
          1. Hard stop  -- intraday low below entry - 2.5 x ATR14(at entry).
          2. EMA20 trail -- once the holding is +15% up, exit when the daily
             close prints below its 20-day EMA (executed next open).
        """
        cfg = self.config
        ts = timestamp or datetime.combine(session_date, cfg.order_deadline)
        signals: List[ExitSignal] = []

        for symbol, pos in holdings.items():
            qty = int(pos.get("quantity", 0))
            if qty <= 0 or symbol not in closes.columns:
                continue
            entry = float(pos.get("entry_price", 0.0))
            stop = float(pos.get("stop_loss", 0.0) or 0.0)
            close = float(closes[symbol].iloc[-1])
            low = float((intraday_lows or {}).get(symbol, close))

            if stop > 0 and low <= stop:
                signals.append(
                    ExitSignal(symbol=symbol, quantity=qty, price=stop,
                               reason="ATR_VOLATILITY_STOP", timestamp=ts)
                )
                continue

            if entry > 0 and (close / entry - 1.0) >= cfg.trail_trigger_gain:
                pos["trail_armed"] = True

            if pos.get("trail_armed"):
                ema20 = float(closes[symbol].ewm(span=cfg.trail_ema, adjust=False).mean().iloc[-1])
                if close < ema20:
                    signals.append(
                        ExitSignal(symbol=symbol, quantity=qty, price=close,
                                   reason="EMA20_TRAILING_STOP", timestamp=ts)
                    )
        return signals
