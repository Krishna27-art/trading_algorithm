"""
Systematic Index Variance Risk Premium Harvest.

STRATEGY CODE : NSE-VRP-INDEX
UNDERLYING    : NIFTY 50 weekly index options (CASH SETTLED -- no physical
                delivery risk, which is exactly why this runs on the index
                and never on single-stock options)
CADENCE       : Thursday 15:10 IST, selling the following week's expiry
STRUCTURE     : defined-risk Iron Condor, 15-delta short strikes protected
                by 5-delta longs

Mechanism
---------
India VIX prices forward variance; Parkinson realised volatility measures
what actually happened. The spread between them (the variance risk premium)
is structurally positive because institutions overpay for index puts as
catastrophe insurance. The engine sells that spread only when it is
statistically rich (60-day z >= 0.5) and the absolute VIX regime is
tradeable (12 <= VIX <= 23).

Hard risk position
------------------
Never naked. Every short leg is bought back against a further OTM long, so
maximum loss is (spread width - net credit) and is known before entry. The
VIX > 23 disengage is an absolute kill switch, not a scaling rule: it is
what stops the engine being short gamma into a 4-June-2024 or March-2020
style gap. If you relax exactly one thing in this file, do not let it be
that.

Data needed
-----------
  * 5-minute NIFTY 50 SPOT OHLC (for Parkinson RV)
  * India VIX daily close
  * NIFTY weekly options chain with per-strike IV or mid price
All three are available on Kite Connect within the 3 req/sec limit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
from config.settings import VRPConfig, settings
from strategy.portfolio_base import (
    ExitSignal,
    OptionLeg,
    OrderSide,
    PortfolioStrategy,
    StructureOrder,
)

try:
    from monitoring.logger import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("vrp_index")


TRADING_DAYS = 252


def bs_delta(
    spot: float,
    strike: float,
    years_to_expiry: float,
    iv: float,
    option_type: str,
    rate: float = 0.065,
) -> float:
    """Black-Scholes delta. Returns +ve for calls, -ve for puts."""
    if years_to_expiry <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * years_to_expiry) / (
        iv * math.sqrt(years_to_expiry)
    )
    ncdf = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    return ncdf if option_type.upper() == "CE" else ncdf - 1.0


def parkinson_volatility(intraday_bars: pd.DataFrame, bars_per_day: Optional[int] = None) -> float:
    """
    Annualised Parkinson realised volatility (in percent) from intraday
    high/low bars of ONE session. Uses the full high-low range of each bar,
    which extracts more information from the same data than close-to-close.

    intraday_bars: columns 'high', 'low' for a single trading day.
    """
    if intraday_bars is None or intraday_bars.empty:
        return float("nan")
    h = intraday_bars["high"].to_numpy(dtype=float)
    l = intraday_bars["low"].to_numpy(dtype=float)
    mask = (h > 0) & (l > 0)
    h, l = h[mask], l[mask]
    if h.size == 0:
        return float("nan")
    n = bars_per_day or h.size
    log_range_sq = np.log(h / l) ** 2
    var = (TRADING_DAYS / (n * 4.0 * math.log(2.0))) * float(log_range_sq.sum())
    return math.sqrt(var) * 100.0 if var > 0 else 0.0


class VRPHarvestStrategy(PortfolioStrategy):
    name = "Systematic Index Variance Risk Premium Harvest"
    strategy_code = "NSE-VRP-INDEX"
    signal_id = "NSE_ALGO_VRP_IDX_V1"

    def __init__(self, config: Optional[VRPConfig] = None):
        if config is not None:
            self.config = config
        else:
            try:
                from config.settings import settings
                self.config = settings.vrp
            except Exception:
                self.config = VRPConfig()

    # ------------------------------------------------------------------ #
    # Schedule
    # ------------------------------------------------------------------ #
    def is_rebalance_day(self, session_date: date) -> bool:
        return session_date.weekday() == self.config.entry_weekday

    # ------------------------------------------------------------------ #
    # Signal
    # ------------------------------------------------------------------ #
    def realised_vol_series(self, intraday_by_day: Dict[date, pd.DataFrame]) -> pd.Series:
        """Per-session Parkinson RV, ordered by date."""
        items = sorted(intraday_by_day.items())
        return pd.Series(
            {d: parkinson_volatility(bars) for d, bars in items}, dtype=float
        ).dropna()

    def vrp_signal(self, rv_series: pd.Series, india_vix: pd.Series) -> Dict[str, float]:
        """
        rv_series  : daily Parkinson RV (percent, annualised), ending t-1.
        india_vix  : India VIX closes on the same dates, ending t-1.

        Returns implied vol, smoothed RV, the raw spread and its 60-day z.
        """
        cfg = self.config
        rv20 = rv_series.ewm(span=cfg.rv_ema_span, adjust=False).mean()
        aligned = pd.concat([india_vix.rename("iv"), rv20.rename("rv20")], axis=1).dropna()
        if len(aligned) < cfg.vrp_zscore_window:
            raise ValueError(
                f"Need {cfg.vrp_zscore_window} aligned sessions for the VRP z-score, "
                f"got {len(aligned)}"
            )
        spread = aligned["iv"] - aligned["rv20"]
        window = spread.iloc[-cfg.vrp_zscore_window:]
        mu, sd = float(window.mean()), float(window.std(ddof=1))
        latest = float(spread.iloc[-1])
        z = 0.0 if (not np.isfinite(sd) or sd == 0) else (latest - mu) / sd
        return {
            "iv": float(aligned["iv"].iloc[-1]),
            "rv20": float(aligned["rv20"].iloc[-1]),
            "spread": latest,
            "z": float(z),
        }

    # ------------------------------------------------------------------ #
    # Structure construction
    # ------------------------------------------------------------------ #
    @staticmethod
    def _years_to_expiry(as_of: date, expiry: date) -> float:
        return max((expiry - as_of).days, 1) / 365.0

    def _pick_strike(
        self,
        chain: pd.DataFrame,
        option_type: str,
        target_delta: float,
        spot: float,
        tau: float,
    ) -> Optional[pd.Series]:
        """Nearest strike by |delta - target| within one option type.
        Uses the chain's own delta column when the broker supplies it,
        otherwise recomputes from the per-strike IV."""
        side = chain[chain["option_type"].str.upper() == option_type.upper()].copy()
        if side.empty:
            return None
        if "delta" in side.columns and side["delta"].notna().all():
            deltas = side["delta"].astype(float)
        else:
            # Sanity-bound IV before feeding it into Black-Scholes: a bad tick
            # (0, negative, or an absurd triple-digit IV) would otherwise
            # produce a garbage delta that still gets picked as "nearest".
            valid_iv = side["iv"].astype(float).between(0.01, 5.0)
            side = side[valid_iv]
            if side.empty:
                return None
            deltas = side.apply(
                lambda r: bs_delta(
                    spot, float(r["strike"]), tau, float(r["iv"]),
                    option_type, self.config.risk_free_rate,
                ),
                axis=1,
            )
        side = side.assign(_delta=deltas)
        side = side[side["_delta"].abs() > 1e-6]
        if side.empty:
            return None
        signed_target = target_delta if option_type.upper() == "CE" else -abs(target_delta)
        idx = (side["_delta"] - signed_target).abs().idxmin()
        return side.loc[idx]

    @staticmethod
    def _price_of(row: pd.Series) -> Optional[float]:
        """Mid of a live two-sided bid/ask quote. Returns None — never a
        fallback to last_price — when there is no tradable quote: a stale
        LTP is not an executable price, and this strategy's max-loss math
        depends on the premium actually being achievable."""
        bid, ask = row.get("bid"), row.get("ask")
        if bid is not None and ask is not None and float(bid) > 0 and float(ask) > 0:
            return (float(bid) + float(ask)) / 2.0
        return None

    def generate_plan(
        self,
        as_of: date,
        spot: float,
        option_chain: pd.DataFrame,
        expiry: date,
        rv_series: pd.Series,
        india_vix: pd.Series,
        timestamp: Optional[datetime] = None,
    ) -> Optional[StructureOrder]:
        """
        option_chain: one row per contract with columns
            tradingsymbol, strike, option_type ('CE'/'PE'), iv,
            and last_price and/or bid+ask. An optional 'delta' column is used
            directly if the broker supplies it.

        Returns None when the regime gates block entry -- an absent trade is
        the expected output most weeks, not a failure.
        """
        cfg = self.config
        ts = timestamp or datetime.combine(as_of, cfg.entry_time)

        sig = self.vrp_signal(rv_series, india_vix)

        if sig["z"] < cfg.min_vrp_zscore:
            logger.info(f"[{self.strategy_code}] no entry {as_of}: VRP z={sig['z']:.2f} below "
                        f"{cfg.min_vrp_zscore}")
            return None
        if not (cfg.vix_floor <= sig["iv"] <= cfg.vix_ceiling):
            logger.info(f"[{self.strategy_code}] no entry {as_of}: India VIX {sig['iv']:.2f} "
                        f"outside [{cfg.vix_floor}, {cfg.vix_ceiling}]")
            return None

        tau = self._years_to_expiry(as_of, expiry)
        picks = {
            "short_call": self._pick_strike(option_chain, "CE", cfg.short_delta, spot, tau),
            "long_call": self._pick_strike(option_chain, "CE", cfg.long_delta, spot, tau),
            "short_put": self._pick_strike(option_chain, "PE", cfg.short_delta, spot, tau),
            "long_put": self._pick_strike(option_chain, "PE", cfg.long_delta, spot, tau),
        }
        if any(v is None for v in picks.values()):
            logger.warning(f"[{self.strategy_code}] chain incomplete on {as_of}; no structure built")
            return None

        # Sanity: protective strikes must sit outside the short strikes, or
        # this is not a defined-risk structure at all.
        if not (float(picks["long_call"]["strike"]) > float(picks["short_call"]["strike"])
                and float(picks["long_put"]["strike"]) < float(picks["short_put"]["strike"])):
            logger.warning(f"[{self.strategy_code}] strike ordering invalid on {as_of}; skipped")
            return None

        legs: List[OptionLeg] = []
        for key, side in (
            ("short_call", OrderSide.SELL),
            ("long_call", OrderSide.BUY),
            ("short_put", OrderSide.SELL),
            ("long_put", OrderSide.BUY),
        ):
            row = picks[key]
            premium = self._price_of(row)
            if premium is None:
                logger.warning(
                    f"[{self.strategy_code}] no live two-sided quote for {row.get('tradingsymbol')} "
                    f"({key}) on {as_of}; refusing to price off a stale last_price — structure skipped"
                )
                return None
            # Slip against ourselves on both sides: sells fill lower, buys higher.
            adj = 1.0 - cfg.premium_slippage_pct if side == OrderSide.SELL else 1.0 + cfg.premium_slippage_pct
            legs.append(
                OptionLeg(
                    tradingsymbol=str(row["tradingsymbol"]),
                    strike=float(row["strike"]),
                    option_type=str(row["option_type"]).upper(),
                    side=side,
                    lots=cfg.lots,
                    lot_size=cfg.lot_size,
                    premium=round(premium * adj, 2),
                    delta=float(row.get("_delta", row.get("delta", 0.0))),
                    expiry=expiry,
                )
            )

        net_credit = sum(leg.signed_premium for leg in legs)
        if net_credit <= 0:
            logger.warning(f"[{self.strategy_code}] structure priced at a net debit on {as_of}; skipped")
            return None

        call_width = float(picks["long_call"]["strike"]) - float(picks["short_call"]["strike"])
        put_width = float(picks["short_put"]["strike"]) - float(picks["long_put"]["strike"])
        qty = cfg.lots * cfg.lot_size
        max_loss = max(call_width, put_width) * qty - net_credit

        return StructureOrder(
            structure_id=f"VRP_IC_{expiry.isoformat()}_{as_of.isoformat()}",
            structure_type="IRON_CONDOR",
            legs=legs,
            timestamp=ts,
            net_credit=round(net_credit, 2),
            max_loss=round(max_loss, 2),
            profit_target=round(net_credit * cfg.profit_target_pct, 2),
            stop_loss=round(net_credit * cfg.stop_loss_multiple, 2),
            expiry=expiry,
            reason="VRP_RICH_DEFINED_RISK_SHORT_VOL",
            metadata={
                "vrp_z": round(sig["z"], 3),
                "india_vix": sig["iv"],
                "rv20_parkinson": round(sig["rv20"], 3),
                "spread": round(sig["spread"], 3),
                "call_width": call_width,
                "put_width": put_width,
                "signal_id": self.signal_id,
            },
        )

    # ------------------------------------------------------------------ #
    # Monitoring
    # ------------------------------------------------------------------ #
    def mark_to_market(
        self, structure: StructureOrder, live_prices: Dict[str, float]
    ) -> Dict[str, float]:
        """Unrealised P&L of the whole structure. Positive = profit.
        A short leg gains as its premium decays; a long leg loses."""
        cost_to_close = 0.0
        for leg in structure.legs:
            px = float(live_prices.get(leg.tradingsymbol, leg.premium))
            sign = 1.0 if leg.side == OrderSide.SELL else -1.0
            cost_to_close += sign * px * leg.quantity
        pnl = structure.net_credit - cost_to_close
        decayed = pnl / structure.net_credit if structure.net_credit else 0.0
        return {"unrealised_pnl": round(pnl, 2), "credit_decayed_pct": round(decayed, 4)}

    def monitor(
        self,
        structure: StructureOrder,
        live_prices: Dict[str, float],
        now: datetime,
    ) -> List[ExitSignal]:
        """
        Three exits, checked in order of severity:
          1. Expiry time stop -- flatten at 14:30 IST on expiry Thursday, to
             sidestep pin risk and settlement-print volatility entirely.
          2. Stop loss  -- unrealised loss >= 1.5x the credit collected.
          3. Profit target -- 65% of the credit has decayed.
        Exits are all-or-nothing: an Iron Condor half-closed is a naked
        vertical, which is not the risk this strategy signed up for.
        """
        cfg = self.config
        mtm = self.mark_to_market(structure, live_prices)
        pnl = mtm["unrealised_pnl"]

        def _all_legs(reason: str, price: float) -> List[ExitSignal]:
            return [
                ExitSignal(
                    symbol=leg.tradingsymbol,
                    quantity=leg.quantity,
                    price=float(live_prices.get(leg.tradingsymbol, leg.premium)),
                    reason=reason,
                    timestamp=now,
                    product="NRML",
                )
                for leg in structure.legs
            ]

        if now.date() >= structure.expiry and now.time() >= cfg.expiry_exit_time:
            return _all_legs("EXPIRY_TIME_STOP", pnl)
        if pnl <= -structure.stop_loss:
            return _all_legs("STRUCTURE_STOP_LOSS", pnl)
        if pnl >= structure.profit_target:
            return _all_legs("PROFIT_TARGET_65PCT_DECAY", pnl)
        return []
