"""
APEX-AIVEM Engine
=================
Merged implementation: AIVEM-Alpha (auction-imbalance + volatility-expansion
momentum, per the uploaded research spec) + a bolted-on catalyst/news factor
(CAT) from the APEX-OPEN design.

Composite score (6 factors, weights re-normalized to sum to 1.0):
    gap        0.24   |z_gap|                       (unsigned magnitude)
    volume     0.19    z_vol                         (unsigned)
    OIR        0.14    sign * z_oir
    sector RS  0.19    sign * z_sector
    market RS  0.10    sign * z_market
    catalyst   0.14    z_cat                         (unsigned - see NOTE)

NOTE on CAT: a material catalyst adds conviction to whichever direction the
gap is already pointing; it does not itself define direction. It is added
unsigned like z_vol, then the whole bracket is multiplied by sign(delta_gap).

Runs against Zerodha Kite Connect (kiteconnect) if installed; if not,
Kite calls are stubbed out via a MockKite so this file still imports and
the scoring/confirmation logic can be unit-tested with synthetic data
(see `if __name__ == "__main__"` at the bottom).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import polars as pl

try:
    from kiteconnect import KiteConnect
except ImportError:  # pragma: no cover - allows the module to be imported/tested without the SDK
    KiteConnect = None  # type: ignore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("apex_aivem")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass
class EngineConfig:
    # Universe / static filters (Stage 0, computed at day t-1 EOD)
    min_adv20_inr: float = 15_00_00_000        # ADV20 >= 15 crore
    min_atr_ratio: float = 0.015                # ATR14/Close >= 1.5%

    # Stage A gating thresholds (computed at 09:12:15 IST)
    gap_atr_min: float = 0.60
    gap_atr_max: float = 2.50
    rvol_pre_min: float = 1.80
    cpr_norm_max: float = 0.85
    circuit_buffer_min: float = 0.025            # >= 2.5% from statutory band
    baseline_preopen_participation: float = 0.015  # ~1.5% of ADV trades in auction (median)

    # Composite score weights (re-normalized to sum to 1.0 after adding CAT)
    w_gap: float = 0.24
    w_vol: float = 0.19
    w_oir: float = 0.14
    w_sector: float = 0.19
    w_market: float = 0.10
    w_cat: float = 0.14

    # Macro (GIFT Nifty) allocation thresholds
    gift_bias_threshold: float = 0.0040          # 0.40%

    # Position selection
    max_positions: int = 4                       # e.g. 2 long / 2 short in neutral regime

    # Stage B confirmation (09:15-09:20 IST 5-min bar)
    min_vol_fraction_of_adv: float = 0.08
    max_wick_ratio: float = 0.35

    # Order mechanics
    tick_size: float = 0.05
    limit_protection_bps: float = 0.0010         # 10 bps

    # Risk / exit
    stop_atr_mult: float = 0.85
    target_atr_mult: float = 1.50
    breakeven_trigger_atr_mult: float = 0.75
    breakeven_lock_bps: float = 0.0012           # 0.12%
    risk_per_trade_frac: float = 0.0075          # 0.75% of equity
    mis_leverage: float = 4.0                    # conservative, SEBI caps at 5.0x intraday

    # Regime governance (India VIX bands)
    vix_halt_below: float = 11.50
    vix_full_deploy_max: float = 22.00
    vix_high_risk_mult: float = 0.5              # halve risk_per_trade above 22
    breakeven_lock_tight_mult: float = 0.50      # tighten trail trigger to 0.50*ATR in high-vol regime

    # Timing (IST, informational — orchestration/scheduling is external to this module)
    preopen_scan_time: str = "09:12:15"
    confirmation_time: str = "09:20:00"
    stale_order_cancel_time: str = "09:25:00"
    terminal_squareoff_time: str = "10:30:00"

    stale_order_window_sec: int = 5 * 60


# --------------------------------------------------------------------------- #
# Catalyst / news scoring (the APEX-OPEN addition)
# --------------------------------------------------------------------------- #

class CatalystScorer:
    """
    Produces a 0-3 catalyst score per symbol from corporate-announcement /
    news headline text:
        0 = no news, 1 = minor, 2 = confirmed material (results/order win/
        rating change), 3 = major (M&A, regulatory action, index rebalance).

    Default implementation is a cheap keyword heuristic so this file runs
    with zero external dependencies. In production, pass an `llm_classify_fn`
    (e.g. a single batched call to an LLM) to replace the heuristic —
    batched once per day across the whole filtered universe, never
    per-stock, per the original design constraint against per-stock LLM
    calls in the scanning loop.
    """

    _MAJOR_KEYWORDS = (
        "merger", "acquisition", "acquire", "delisting", "sebi", "regulatory action",
        "index inclusion", "index exclusion", "ban", "fraud", "insolvency",
        "resignation of ceo", "resignation of md",
    )
    _MATERIAL_KEYWORDS = (
        "results", "quarterly results", "q1 ", "q2 ", "q3 ", "q4 ",
        "order win", "contract win", "rating upgrade", "rating downgrade",
        "rating change", "block deal", "bulk deal", "stake sale",
        "capacity expansion", "dividend", "buyback",
    )
    _MINOR_KEYWORDS = (
        "board meeting", "investor call", "clarification", "press release",
    )

    def __init__(self, llm_classify_fn: Optional[Callable] = None):
        self.llm_classify_fn = llm_classify_fn

    def score(self, symbol: str, headlines: list[str]) -> int:
        if not headlines:
            return 0
        if self.llm_classify_fn is not None:
            try:
                return int(self.llm_classify_fn(symbol, headlines))
            except TypeError:
                res = self.llm_classify_fn({symbol: headlines})
                if isinstance(res, dict):
                    return int(res.get(symbol, 0))
        text = " | ".join(h.lower() for h in headlines)
        if any(k in text for k in self._MAJOR_KEYWORDS):
            return 3
        if any(k in text for k in self._MATERIAL_KEYWORDS):
            return 2
        if any(k in text for k in self._MINOR_KEYWORDS):
            return 1
        return 0

    def score_batch(self, announcements: dict[str, list[str]]) -> dict[str, int]:
        """Single pass over the whole filtered universe — not one call per stock."""
        if self.llm_classify_fn is not None:
            try:
                res = self.llm_classify_fn(announcements)
                if isinstance(res, dict):
                    return {sym: int(res.get(sym, 0)) for sym in announcements}
            except TypeError:
                pass
        return {sym: self.score(sym, heads) for sym, heads in announcements.items()}


# --------------------------------------------------------------------------- #
# Regime governance
# --------------------------------------------------------------------------- #

@dataclass
class RegimeDecision:
    trading_enabled: bool
    risk_per_trade_frac: float
    breakeven_trigger_atr_mult: float
    reason: str


def evaluate_regime(cfg: EngineConfig, india_vix: float, calendar_blackout: bool) -> RegimeDecision:
    if calendar_blackout:
        return RegimeDecision(False, 0.0, cfg.breakeven_trigger_atr_mult,
                               "Scheduled binary macro event (Budget/Election/RBI MPC) — hard blackout")
    if india_vix < cfg.vix_halt_below:
        return RegimeDecision(False, 0.0, cfg.breakeven_trigger_atr_mult,
                               f"India VIX {india_vix:.2f} < {cfg.vix_halt_below}: low-vol noise regime, halt")
    if india_vix <= cfg.vix_full_deploy_max:
        return RegimeDecision(True, cfg.risk_per_trade_frac, cfg.breakeven_trigger_atr_mult,
                               f"India VIX {india_vix:.2f}: normal trending regime, full deployment")
    reduced_risk = cfg.risk_per_trade_frac * cfg.vix_high_risk_mult
    tight_trail = cfg.breakeven_trigger_atr_mult * cfg.breakeven_lock_tight_mult
    return RegimeDecision(True, reduced_risk, tight_trail,
                           f"India VIX {india_vix:.2f} > {cfg.vix_full_deploy_max}: high-vol, risk halved")


# --------------------------------------------------------------------------- #
# Main engine
# --------------------------------------------------------------------------- #

class ApexAivemEngine:
    """
    Pre-market scanning -> cross-sectional ranking (incl. catalyst factor)
    -> 5-minute confirmation -> bracket order dispatch -> 10:30 IST
    terminal square-off, on NSE cash equities via Zerodha Kite Connect.
    """

    def __init__(
        self,
        api_key: str,
        access_token: str,
        pre_screened_symbols: list[str],
        config: Optional[EngineConfig] = None,
        catalyst_scorer: Optional[CatalystScorer] = None,
        kite_client=None,
    ):
        self.cfg = config or EngineConfig()
        self.symbols = pre_screened_symbols          # ~250-350 post Stage-0 filter
        self.catalyst_scorer = catalyst_scorer or CatalystScorer()
        self.static_eod: dict[str, dict] = {}
        self.active_orders: dict[str, str] = {}
        self.active_positions: dict[str, dict] = {}
        self.regime: Optional[RegimeDecision] = None

        if kite_client is not None:
            self.kite = kite_client
        elif KiteConnect is not None:
            self.kite = KiteConnect(api_key=api_key)
            self.kite.set_access_token(access_token)
        else:  # pragma: no cover
            raise RuntimeError("kiteconnect not installed and no kite_client provided")

    # ------------------------------------------------------------------ #
    # Stage 0: static EOD load
    # ------------------------------------------------------------------ #

    def load_eod_features(self, eod_data: dict[str, dict]) -> None:
        """
        eod_data[symbol] = {"atr_14": float, "adv_20": float, "sector": str,
                             "beta": float, "cpr_norm": float}
        Computed at Day t-1 16:00 IST from Bhavcopy + your own rolling stats.
        """
        self.static_eod = eod_data
        log.info("Loaded static EOD parameters for %d instruments.", len(self.static_eod))

    # ------------------------------------------------------------------ #
    # Regime governance
    # ------------------------------------------------------------------ #

    def set_regime(self, india_vix: float, calendar_blackout: bool = False) -> RegimeDecision:
        self.regime = evaluate_regime(self.cfg, india_vix, calendar_blackout)
        log.info("Regime: %s", self.regime.reason)
        return self.regime

    # ------------------------------------------------------------------ #
    # Stage A: pre-market scan, scoring, ranking (09:12:15 IST)
    # ------------------------------------------------------------------ #

    def run_pre_market_scan(
        self,
        gift_nifty_gap: float,
        catalyst_scores: Optional[dict[str, int]] = None,
        quotes: Optional[dict[str, dict]] = None,
    ) -> tuple[list[dict], list[dict]]:
        """
        quotes: symbol -> raw Kite /quote() response dict. If None, this
        method fetches live quotes via self.kite.quote() in batches of 450
        (Kite's per-call symbol cap), sleeping 1.05s between calls to
        respect the 1 req/sec historical/quote rate limit.
        """
        if catalyst_scores is None:
            catalyst_scores = {}
        if self.regime is not None and not self.regime.trading_enabled:
            log.warning("Regime halts trading today (%s). Skipping scan.", self.regime.reason)
            return [], []

        if quotes is None:
            quotes = self._fetch_quotes_batched(self.symbols)

        cfg = self.cfg
        records = []
        for sym, q in quotes.items():
            eod = self.static_eod.get(sym)
            if eod is None:
                continue

            p_pre = q["ohlc"]["open"]
            c_prev = q["ohlc"]["close"]
            vol_pre = q["volume"]            # share volume from Kite quote
            value_pre = vol_pre * p_pre       # INR value traded pre-market — adv_20 below is INR, not shares
            buy_q = q.get("total_buy_quantity", 0)
            sell_q = q.get("total_sell_quantity", 0)
            circuit_limit = q.get("lower_circuit_limit", 0.0)

            dist_to_circuit = abs(p_pre - circuit_limit) / p_pre if circuit_limit > 0 else 1.0
            if dist_to_circuit < cfg.circuit_buffer_min:
                continue

            records.append({
                "symbol": sym,
                "p_pre": float(p_pre),
                "c_prev": float(c_prev),
                "vol_pre": float(vol_pre),
                "value_pre": float(value_pre),
                "buy_q": float(buy_q),
                "sell_q": float(sell_q),
                "atr_14": float(eod["atr_14"]),
                "adv_20": float(eod["adv_20"]),
                "sector": str(eod["sector"]),
                "beta": float(eod["beta"]),
                "cpr_norm": float(eod["cpr_norm"]),
                "cat_score": float(catalyst_scores.get(sym, 0)),
            })

        if not records:
            log.warning("No pre-market quote data assembled.")
            return [], []

        df = pl.DataFrame(records)

        df = df.with_columns([
            ((pl.col("p_pre") - pl.col("c_prev")) / pl.col("atr_14")).alias("delta_gap"),
            # adv_20 is a rupee-value ADV (min_adv20_inr), so rvol must compare like
            # units: rupee value traded pre-market against rupee ADV, not raw share
            # volume against a rupee figure.
            (pl.col("value_pre") / (cfg.baseline_preopen_participation * pl.col("adv_20"))).alias("rvol_pre"),
            ((pl.col("buy_q") - pl.col("sell_q")) / (pl.col("buy_q") + pl.col("sell_q") + 1.0)).alias("oir"),
            (((pl.col("p_pre") - pl.col("c_prev")) / pl.col("c_prev")) - (pl.col("beta") * gift_nifty_gap)).alias("rs_mkt"),
        ])
        df = df.with_columns((pl.col("rvol_pre").clip(0.01, 100.0).log()).alias("v_norm"))

        sec_medians = df.group_by("sector").agg(pl.col("delta_gap").median().alias("sec_median"))
        df = df.join(sec_medians, on="sector").with_columns(
            (pl.col("delta_gap") - pl.col("sec_median")).alias("rs_sector")
        )

        gated = df.filter(
            (pl.col("delta_gap").abs() >= cfg.gap_atr_min) &
            (pl.col("delta_gap").abs() <= cfg.gap_atr_max) &
            (pl.col("rvol_pre") >= cfg.rvol_pre_min) &
            (pl.col("cpr_norm") <= cfg.cpr_norm_max)
        )

        if gated.height < cfg.max_positions:
            log.warning("Insufficient candidates passed Stage A gating (%d < %d).",
                        gated.height, cfg.max_positions)
            return [], []

        def z(col_name: str) -> pl.Expr:
            mean = pl.col(col_name).mean()
            std = pl.col(col_name).std()
            return ((pl.col(col_name) - mean) / (std + 1e-6)).clip(-3.0, 3.0)

        scored = gated.with_columns([
            z("delta_gap").alias("z_gap"),
            z("v_norm").alias("z_vol"),
            z("oir").alias("z_oir"),
            z("rs_sector").alias("z_sec"),
            z("rs_mkt").alias("z_mkt"),
            z("cat_score").alias("z_cat"),
        ]).with_columns(
            (
                pl.col("delta_gap").sign() * (
                    cfg.w_gap * pl.col("z_gap").abs() +
                    cfg.w_vol * pl.col("z_vol") +
                    cfg.w_oir * (pl.col("delta_gap").sign() * pl.col("z_oir")) +
                    cfg.w_sector * (pl.col("delta_gap").sign() * pl.col("z_sec")) +
                    cfg.w_market * (pl.col("delta_gap").sign() * pl.col("z_mkt")) +
                    cfg.w_cat * pl.col("z_cat")
                ) * (1.0 - 0.10 * pl.col("cpr_norm").clip(0.0, 1.5))
            ).alias("apex_score")
        )

        longs = scored.filter(pl.col("apex_score") > 0).sort("apex_score", descending=True)
        shorts = scored.filter(pl.col("apex_score") < 0).sort("apex_score", descending=False)

        half = cfg.max_positions // 2
        if gift_nifty_gap > cfg.gift_bias_threshold and longs.height >= cfg.max_positions:
            selected_longs, selected_shorts = longs.head(cfg.max_positions).to_dicts(), []
        elif gift_nifty_gap < -cfg.gift_bias_threshold and shorts.height >= cfg.max_positions:
            selected_longs, selected_shorts = [], shorts.head(cfg.max_positions).to_dicts()
        else:
            selected_longs = longs.head(half).to_dicts()
            selected_shorts = shorts.head(half).to_dicts()

        log.info("Stage A selection: %d longs, %d shorts.", len(selected_longs), len(selected_shorts))
        return selected_longs, selected_shorts

    def _fetch_quotes_batched(self, symbols: list[str]) -> dict[str, dict]:
        batch_size = 450
        out: dict[str, dict] = {}
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            res = self.kite.quote(batch)
            out.update(res)
            if i + batch_size < len(symbols):
                time.sleep(1.05)  # Kite: 1 request/second on /quote
        return out

    # ------------------------------------------------------------------ #
    # Stage B: 5-minute opening confirmation (09:15-09:20 IST)
    # ------------------------------------------------------------------ #

    def evaluate_stage_b_confirmation(self, sym: str, direction: str, bar_5m: dict) -> bool:
        cfg = self.cfg
        o, h, l, c = bar_5m["open"], bar_5m["high"], bar_5m["low"], bar_5m["close"]
        vol, vwap = bar_5m["volume"], bar_5m["vwap"]
        adv_20 = self.static_eod[sym]["adv_20"]

        # adv_20 is rupee-value ADV — compare against rupee value traded in the
        # bar (vol * close), not raw share volume, or this gate is unit-mismatched.
        value_5m = vol * c
        if value_5m < cfg.min_vol_fraction_of_adv * adv_20:
            return False

        range_len = max(h - l, cfg.tick_size)

        if direction == "LONG":
            wick_ratio = (h - c) / range_len
            return (c > o) and (c > vwap) and (wick_ratio <= cfg.max_wick_ratio)
        elif direction == "SHORT":
            wick_ratio = (c - l) / range_len
            return (c < o) and (c < vwap) and (wick_ratio <= cfg.max_wick_ratio)
        return False

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #

    def dispatch_execution_bracket(self, candidate: dict, direction: str, bar_5m: dict, equity: float) -> None:
        cfg = self.cfg
        sym = candidate["symbol"]
        atr = candidate["atr_14"]

        risk_frac = self.regime.risk_per_trade_frac if self.regime else cfg.risk_per_trade_frac
        risk_capital = equity * risk_frac
        if risk_capital <= 0:
            log.info("Regime risk fraction is zero — skipping %s.", sym)
            return

        if direction == "LONG":
            trigger_price = round(bar_5m["high"] + cfg.tick_size, 2)
            limit_price = round(trigger_price * (1.0 + cfg.limit_protection_bps), 2)
            sl_price = round(max(bar_5m["low"], trigger_price - cfg.stop_atr_mult * atr), 2)
            target_price = round(trigger_price + cfg.target_atr_mult * atr, 2)
            risk_per_share = trigger_price - sl_price
            order_side = self.kite.TRANSACTION_TYPE_BUY
        else:
            trigger_price = round(bar_5m["low"] - cfg.tick_size, 2)
            limit_price = round(trigger_price * (1.0 - cfg.limit_protection_bps), 2)
            sl_price = round(min(bar_5m["high"], trigger_price + cfg.stop_atr_mult * atr), 2)
            target_price = round(trigger_price - cfg.target_atr_mult * atr, 2)
            risk_per_share = sl_price - trigger_price
            order_side = self.kite.TRANSACTION_TYPE_SELL

        if risk_per_share <= 0:
            log.warning("Non-positive risk_per_share for %s — skipping.", sym)
            return

        raw_qty = int(risk_capital / risk_per_share)
        max_mis_qty = int((equity * cfg.mis_leverage) / (cfg.max_positions * trigger_price))
        if raw_qty < 1 or max_mis_qty < 1:
            # Forcing a 1-unit order here (the old max(1, ...) behaviour) can put
            # on a position whose risk exceeds risk_per_trade_frac of equity, or
            # exceeds the leverage cap outright. Skip instead of silently
            # breaching the risk limit.
            log.info(
                "Computed order quantity is zero for %s (risk-sized=%d, leverage-capped=%d) — skipping.",
                sym, raw_qty, max_mis_qty,
            )
            return
        final_qty = min(raw_qty, max_mis_qty)

        log.info(
            "DISPATCH %s %s qty=%d trigger=%.2f limit=%.2f sl=%.2f target=%.2f cat=%.0f",
            direction, sym, final_qty, trigger_price, limit_price, sl_price, target_price,
            candidate.get("cat_score", 0),
        )

        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NSE,
                tradingsymbol=sym.replace("NSE:", ""),
                transaction_type=order_side,
                quantity=final_qty,
                product=self.kite.PRODUCT_MIS,
                order_type=self.kite.ORDER_TYPE_SL,
                price=limit_price,
                trigger_price=trigger_price,
            )
            self.active_orders[sym] = order_id
            self.active_positions[sym] = {
                "direction": direction, "qty": final_qty, "entry": trigger_price,
                "sl": sl_price, "target": target_price, "trail_active": False,
                "atr": atr, "order_placed_at": time.time(),
                "filled": False, "sl_order_id": None,
            }
        except Exception as exc:  # noqa: BLE001
            log.error("Execution failed for %s: %s", sym, exc)

    def _order_status(self, order_id: str) -> tuple[str, float]:
        """Return (last_status, filled_quantity) from the broker's own order
        history. Never inferred from local state — the broker is the source
        of truth for whether an order actually filled."""
        try:
            history = self.kite.order_history(order_id)
        except Exception as exc:  # noqa: BLE001
            log.error("order_history lookup failed for order %s: %s", order_id, exc)
            return "UNKNOWN", 0.0
        if not history:
            return "UNKNOWN", 0.0
        last = history[-1]
        return str(last.get("status", "UNKNOWN")), float(last.get("filled_quantity", 0) or 0)

    def _place_protective_stop(self, sym: str, pos: dict) -> None:
        """Place a broker-side SL-M order at pos['sl'] so the internal stop
        is actually market protection, not just a number we track locally."""
        exit_side = (self.kite.TRANSACTION_TYPE_SELL if pos["direction"] == "LONG"
                     else self.kite.TRANSACTION_TYPE_BUY)
        try:
            sl_order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NSE,
                tradingsymbol=sym.replace("NSE:", ""),
                transaction_type=exit_side,
                quantity=pos["qty"],
                product=self.kite.PRODUCT_MIS,
                order_type=getattr(self.kite, "ORDER_TYPE_SLM", self.kite.ORDER_TYPE_SL),
                trigger_price=round(pos["sl"], 2),
            )
            pos["sl_order_id"] = sl_order_id
            log.info("Protective SL order placed for %s at %.2f (order_id=%s).", sym, pos["sl"], sl_order_id)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Protective SL placement FAILED for %s — position is unprotected on the broker "
                "side until this is retried: %s", sym, exc,
            )

    def purge_stale_orders(self, now_epoch: Optional[float] = None) -> None:
        """Reconcile every tracked entry order against the broker's own
        status (never assumed): confirm real fills and attach broker-side
        protection, or cancel genuinely-stale unfilled orders."""
        now_epoch = now_epoch if now_epoch is not None else time.time()
        for sym, pos in list(self.active_positions.items()):
            if pos.get("filled"):
                continue

            order_id = self.active_orders.get(sym)
            status, filled_qty = self._order_status(order_id) if order_id else ("UNKNOWN", 0.0)

            if status == "COMPLETE":
                # COMPLETE means fully filled per Kite's own order-status semantics.
                pos["filled"] = True
                self._place_protective_stop(sym, pos)
                continue

            if status in ("REJECTED", "CANCELLED"):
                if filled_qty > 0:
                    log.warning(
                        "Entry order for %s was %s after partially filling %.0f of %d — "
                        "treating the filled quantity as a live position needing protection.",
                        sym, status, filled_qty, pos["qty"],
                    )
                    pos["qty"] = int(filled_qty)
                    pos["filled"] = True
                    self._place_protective_stop(sym, pos)
                else:
                    log.warning("Entry order for %s was %s by the broker — dropping tracking.", sym, status)
                    self.active_positions.pop(sym, None)
                    self.active_orders.pop(sym, None)
                continue

            if now_epoch - pos["order_placed_at"] > self.cfg.stale_order_window_sec:
                if order_id:
                    try:
                        self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=order_id)
                        log.info("Cancelled stale unfilled order for %s.", sym)
                    except Exception as exc:  # noqa: BLE001
                        log.error("Cancel failed for %s: %s", sym, exc)
                self.active_positions.pop(sym, None)
                self.active_orders.pop(sym, None)

    def monitor_dynamic_risk(self, ltp_by_symbol: dict[str, float]) -> None:
        """Call repeatedly 09:20-10:29:59 IST with latest LTPs; trails stop to breakeven."""
        trigger_mult = self.regime.breakeven_trigger_atr_mult if self.regime else self.cfg.breakeven_trigger_atr_mult
        for sym, pos in self.active_positions.items():
            if not pos.get("filled") or pos.get("trail_active") or sym not in ltp_by_symbol:
                continue
            ltp = ltp_by_symbol[sym]
            favorable_move = (ltp - pos["entry"]) if pos["direction"] == "LONG" else (pos["entry"] - ltp)
            if favorable_move >= trigger_mult * pos["atr"]:
                lock = pos["entry"] * self.cfg.breakeven_lock_bps
                new_sl = pos["entry"] + lock if pos["direction"] == "LONG" else pos["entry"] - lock
                pos["sl"] = new_sl
                pos["trail_active"] = True
                sl_order_id = pos.get("sl_order_id")
                if sl_order_id:
                    try:
                        self.kite.modify_order(
                            variety=self.kite.VARIETY_REGULAR,
                            order_id=sl_order_id,
                            trigger_price=round(new_sl, 2),
                        )
                        log.info("Breakeven trail activated for %s: broker SL modified -> %.2f", sym, new_sl)
                    except Exception as exc:  # noqa: BLE001
                        log.error(
                            "Broker SL modify FAILED for %s (order_id=%s) — internal sl updated to %.2f "
                            "but the LIVE broker stop is still at the old level: %s",
                            sym, sl_order_id, new_sl, exc,
                        )
                else:
                    log.warning(
                        "Breakeven trail activated for %s but no protective sl_order_id is tracked — "
                        "internal sl updated to %.2f with no broker-side order to modify.", sym, new_sl,
                    )

    def enforce_terminal_squareoff(self) -> None:
        log.info("10:30:00 IST — terminal square-off.")
        unresolved: dict[str, dict] = {}
        for sym, pos in list(self.active_positions.items()):
            exit_side = (self.kite.TRANSACTION_TYPE_SELL if pos["direction"] == "LONG"
                         else self.kite.TRANSACTION_TYPE_BUY)

            # Cancel the resting protective SL first — leaving it live after
            # we also send a market exit risks a second, unwanted fill.
            sl_order_id = pos.get("sl_order_id")
            if sl_order_id:
                try:
                    self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=sl_order_id)
                except Exception as exc:  # noqa: BLE001
                    log.error("Could not cancel protective SL for %s before square-off: %s", sym, exc)

            try:
                exit_order_id = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=self.kite.EXCHANGE_NSE,
                    tradingsymbol=sym.replace("NSE:", ""),
                    transaction_type=exit_side,
                    quantity=pos["qty"],
                    product=self.kite.PRODUCT_MIS,
                    order_type=self.kite.ORDER_TYPE_MARKET,
                )
                log.info("Square-off sent for %s (order_id=%s).", sym, exit_order_id)
            except Exception as exc:  # noqa: BLE001
                log.error("Square-off order placement FAILED for %s — position may still be live: %s", sym, exc)
                unresolved[sym] = pos
                continue

            status, filled_qty = self._order_status(exit_order_id)
            if status != "COMPLETE" or filled_qty < pos["qty"]:
                log.error(
                    "CRITICAL: square-off for %s not confirmed filled by the broker "
                    "(status=%s, filled=%.0f/%d). Retaining internal tracking for manual follow-up "
                    "instead of assuming the exit succeeded.",
                    sym, status, filled_qty, pos["qty"],
                )
                unresolved[sym] = pos
        self.active_positions.clear()
        self.active_orders.clear()
        if unresolved:
            self.active_positions.update(unresolved)
            log.error(
                "%d position(s) still require manual confirmation after terminal square-off: %s",
                len(unresolved), list(unresolved.keys()),
            )


# --------------------------------------------------------------------------- #
# Mock Kite (used only when kiteconnect isn't installed / for offline tests)
# --------------------------------------------------------------------------- #

class MockKite:
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"
    VARIETY_REGULAR = "regular"
    EXCHANGE_NSE = "NSE"
    PRODUCT_MIS = "MIS"
    ORDER_TYPE_SL = "SL"
    ORDER_TYPE_SLM = "SL-M"
    ORDER_TYPE_MARKET = "MARKET"

    def __init__(self):
        self._next_id = 0

    def _new_id(self) -> str:
        self._next_id += 1
        return f"MOCK-ORDER-{self._next_id}"

    def place_order(self, **kwargs):
        log.info("[MockKite] place_order(%s)", kwargs)
        return self._new_id()

    def cancel_order(self, **kwargs):
        log.info("[MockKite] cancel_order(%s)", kwargs)

    def modify_order(self, **kwargs):
        log.info("[MockKite] modify_order(%s)", kwargs)

    def order_history(self, order_id):
        # Offline/demo stub: treat every order as immediately, fully filled
        # so the reconciliation path can be exercised without a live broker.
        return [{"order_id": order_id, "status": "COMPLETE", "filled_quantity": 10**9}]

    def quote(self, symbols):
        raise NotImplementedError("Supply `quotes=` directly in offline/demo mode")


if __name__ == "__main__":
    rng = np.random.default_rng(7)
    n = 40
    symbols = [f"NSE:STOCK{i:02d}" for i in range(n)]
    sectors = rng.choice(["IT", "BANK", "PHARMA", "AUTO"], size=n)

    eod = {
        sym: {
            "atr_14": float(rng.uniform(5, 40)),
            "adv_20": float(rng.uniform(5e7, 5e8)),
            "sector": sectors[i],
            "beta": float(rng.uniform(0.6, 1.4)),
            "cpr_norm": float(rng.uniform(0.2, 1.2)),
        }
        for i, sym in enumerate(symbols)
    }

    quotes = {}
    for sym in symbols:
        atr = eod[sym]["atr_14"]
        c_prev = float(rng.uniform(200, 3000))
        gap = rng.normal(0, 1.2) * atr
        quotes[sym] = {
            "ohlc": {"open": c_prev + gap, "close": c_prev},
            "volume": float(rng.uniform(0, 3) * 0.015 * eod[sym]["adv_20"]),
            "total_buy_quantity": float(rng.uniform(1000, 50000)),
            "total_sell_quantity": float(rng.uniform(1000, 50000)),
            "lower_circuit_limit": 0.0,
        }

    catalyst_scores = {sym: int(rng.choice([0, 0, 0, 1, 2, 3])) for sym in symbols}

    engine = ApexAivemEngine(
        api_key="dummy", access_token="dummy",
        pre_screened_symbols=symbols, kite_client=MockKite(),
    )
    engine.load_eod_features(eod)
    engine.set_regime(india_vix=16.5, calendar_blackout=False)

    longs, shorts = engine.run_pre_market_scan(
        gift_nifty_gap=0.001, catalyst_scores=catalyst_scores, quotes=quotes,
    )
    print(f"\nLongs selected: {[c['symbol'] for c in longs]}")
    print(f"Shorts selected: {[c['symbol'] for c in shorts]}")

    if longs:
        cand = longs[0]
        bar = {
            "open": cand["p_pre"], "high": cand["p_pre"] * 1.004,
            "low": cand["p_pre"] * 0.998, "close": cand["p_pre"] * 1.003,
            "volume": 0.09 * cand["adv_20"], "vwap": cand["p_pre"] * 1.001,
        }
        confirmed = engine.evaluate_stage_b_confirmation(cand["symbol"], "LONG", bar)
        print(f"Stage B confirmation for {cand['symbol']}: {confirmed}")
        if confirmed:
            engine.dispatch_execution_bracket(cand, "LONG", bar, equity=10_00_000)
            engine.purge_stale_orders()  # confirms the fill + places the protective SL, per MockKite
            engine.monitor_dynamic_risk({cand["symbol"]: bar["close"] * 1.01})
            engine.enforce_terminal_squareoff()
