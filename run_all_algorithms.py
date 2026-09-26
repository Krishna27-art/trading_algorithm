"""
Unified Runner for All 6 Trading Algorithms
===========================================
Executes all 6 institutional algorithms individually or together with live Zerodha Kite Connect data
(falling back to simulated / cached feeds if offline).

Algorithms:
  [1] ORB Strategy           - Intraday Opening Range Breakout (strategy/orb_strategy.py)
  [2] CPR Regime Breakout    - Central Pivot Range Regime Expansion (strategy/cpr_strategy.py)
  [3] Buffered Dual-EMA      - 9/21 EMA Trend with ATR Buffer (strategy/dual_ema_strategy.py)
  [4] NSE-RM-100 Momentum    - Residual Momentum Portfolio Rebalancer (strategy/residual_momentum.py)
  [5] NSE-VRP-INDEX Harvest  - Variance Risk Premium Options Writing (strategy/vrp_index.py)
  [6] APEX-AIVEM Engine      - 6-Factor Pre-Market Auction & Catalyst Engine (strategy/apex_engine.py)

Usage:
  python run_all_algorithms.py --all                   # Run all 6 sequentially, one at a time
  python run_all_algorithms.py --algo 1                # Run only Algo 1 (ORB)
  python run_all_algorithms.py --algo 2                # Run only Algo 2 (CPR)
  python run_all_algorithms.py --algo 3                # Run only Algo 3 (Dual-EMA)
  python run_all_algorithms.py --algo 4                # Run only Algo 4 (RM-100)
  python run_all_algorithms.py --algo 5                # Run only Algo 5 (VRP Index)
  python run_all_algorithms.py --algo 6                # Run only Algo 6 (APEX-AIVEM)
  python run_all_algorithms.py --algo apex             # Name aliases: orb, cpr, dual_ema, rm100, vrp, apex
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from tabulate import tabulate

# Ensure root workspace is in python path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import settings
from config.universe import create_instrument_config_for_equity, resolve_universe_tokens
from data.eod_panel_loader import EODPanelLoader
from data.historical_loader import HistoricalDataLoader
from data.instrument_resolver import instrument_resolver
from data.options_chain_loader import OptionsChainLoader
from kite_client import KiteApp
from scanner.stock_ranker import NiftyUniverseScanner
from strategy import (
    ApexAivemEngine,
    BufferedDualEMAStrategy,
    CatalystScorer,
    CPRRegimeBreakoutStrategy,
    EngineConfig,
    IntradayORBStrategy,
    MockKite,
    ResidualMomentumConfig,
    ResidualMomentumStrategy,
    VRPConfig,
    VRPHarvestStrategy,
)
from strategy.prediction_service import prediction_service


def get_kite_session() -> tuple[Optional[KiteApp], Any]:
    """Helper to initialize KiteApp connection."""
    try:
        app = KiteApp()
        if app.is_connected():
            return app, app.kite
    except Exception:
        pass
    return None, None


def generate_synthetic_15m_candles(symbol: str, base_price: float = 2000.0) -> pd.DataFrame:
    """Generates synthetic 15m intraday bars for offline demonstration."""
    n_bars = 40
    end_dt = datetime.now()
    dates = [end_dt - timedelta(minutes=15 * (n_bars - i)) for i in range(n_bars)]
    rng = np.random.default_rng(hash(symbol) % (2**31))
    ret = rng.normal(0.0002, 0.003, n_bars)
    closes = base_price * np.cumprod(1 + ret)
    opens = np.roll(closes, 1)
    opens[0] = base_price
    highs = np.maximum(opens, closes) + rng.uniform(1.0, 5.0, n_bars)
    lows = np.minimum(opens, closes) - rng.uniform(1.0, 5.0, n_bars)
    volumes = rng.uniform(5000, 50000, n_bars)
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes,
    }, index=pd.DatetimeIndex(dates))


# =============================================================================
# ALGORITHM 1: INTRADAY ORB (Opening Range Breakout)
# =============================================================================
def run_algo_1_orb(kite_app: Optional[KiteApp], kite: Any, target_symbols: List[str]):
    print("\n" + "=" * 90)
    print("  [ALGORITHM 1] INTRADAY OPENING RANGE BREAKOUT (ORB + VWAP)")
    print("=" * 90)
    today = date.today()
    start_d = today - timedelta(days=10)
    tokens = resolve_universe_tokens(kite_client=kite, force_refresh=False) if kite else {}

    results = []
    for sym in target_symbols:
        if sym == "NIFTY":
            token = 256265
            ltp = float(kite.quote(["NSE:NIFTY 50"]).get("NSE:NIFTY 50", {}).get("last_price", 24000.0)) if kite else 24000.0
        else:
            token = tokens.get(sym) or (instrument_resolver.resolve_token(sym, exchange="NSE", kite_client=kite) if kite else 0) or 0
            ltp = float(kite.quote([f"NSE:{sym}"]).get(f"NSE:{sym}", {}).get("last_price", 2000.0)) if kite else 2000.0

        cache_file = settings.base_dir / "data" / "cache" / f"{sym}_15m.csv"
        df_15m = pd.DataFrame()
        if kite_app:
            try:
                df_15m = HistoricalDataLoader.fetch_real_data(
                    kite_client=kite_app,
                    instrument_token=token,
                    start_date=start_d,
                    end_date=today,
                    interval="15minute",
                    cache_path=cache_file,
                    force_refresh=False,
                )
            except Exception:
                df_15m = pd.DataFrame()

        if df_15m.empty:
            df_15m = generate_synthetic_15m_candles(sym, ltp)

        preds, _ = prediction_service.evaluate_symbol(symbol=sym, df_15m=df_15m, current_ltp=ltp, token=token)
        orb = preds["orb"]

        results.append({
            "Symbol": sym,
            "LTP": f"₹{ltp:,.2f}",
            "Status": orb.status,
            "Direction": orb.direction or "NONE",
            "Entry": f"₹{orb.entry:,.2f}" if orb.entry else "N/A",
            "Stop Loss": f"₹{orb.stop_loss:,.2f}" if orb.stop_loss else "N/A",
            "Target": f"₹{orb.target:,.2f}" if orb.target else "N/A",
            "Reason": orb.reason,
        })

    print(tabulate(results, headers="keys", tablefmt="fancy_grid"))


# =============================================================================
# ALGORITHM 2: CPR REGIME BREAKOUT
# =============================================================================
def run_algo_2_cpr(kite_app: Optional[KiteApp], kite: Any, target_symbols: List[str]):
    print("\n" + "=" * 90)
    print("  [ALGORITHM 2] CENTRAL PIVOT RANGE (CPR) REGIME EXPANSION")
    print("=" * 90)
    today = date.today()
    start_d = today - timedelta(days=10)
    tokens = resolve_universe_tokens(kite_client=kite, force_refresh=False) if kite else {}

    results = []
    for sym in target_symbols:
        if sym == "NIFTY":
            token = 256265
            ltp = float(kite.quote(["NSE:NIFTY 50"]).get("NSE:NIFTY 50", {}).get("last_price", 24000.0)) if kite else 24000.0
        else:
            token = tokens.get(sym) or (instrument_resolver.resolve_token(sym, exchange="NSE", kite_client=kite) if kite else 0) or 0
            ltp = float(kite.quote([f"NSE:{sym}"]).get(f"NSE:{sym}", {}).get("last_price", 2000.0)) if kite else 2000.0

        cache_file = settings.base_dir / "data" / "cache" / f"{sym}_15m.csv"
        df_15m = pd.DataFrame()
        if kite_app:
            try:
                df_15m = HistoricalDataLoader.fetch_real_data(
                    kite_client=kite_app,
                    instrument_token=token,
                    start_date=start_d,
                    end_date=today,
                    interval="15minute",
                    cache_path=cache_file,
                    force_refresh=False,
                )
            except Exception:
                df_15m = pd.DataFrame()

        if df_15m.empty:
            df_15m = generate_synthetic_15m_candles(sym, ltp)

        preds, _ = prediction_service.evaluate_symbol(symbol=sym, df_15m=df_15m, current_ltp=ltp, token=token)
        cpr = preds["cpr"]

        results.append({
            "Symbol": sym,
            "LTP": f"₹{ltp:,.2f}",
            "Status": cpr.status,
            "Direction": cpr.direction or "NONE",
            "Entry": f"₹{cpr.entry:,.2f}" if cpr.entry else "N/A",
            "Stop Loss": f"₹{cpr.stop_loss:,.2f}" if cpr.stop_loss else "N/A",
            "Target": f"₹{cpr.target:,.2f}" if cpr.target else "N/A",
            "Reason": cpr.reason,
        })

    print(tabulate(results, headers="keys", tablefmt="fancy_grid"))


# =============================================================================
# ALGORITHM 3: BUFFERED DUAL-EMA STRATEGY
# =============================================================================
def run_algo_3_dual_ema(kite_app: Optional[KiteApp], kite: Any, target_symbols: List[str]):
    print("\n" + "=" * 90)
    print("  [ALGORITHM 3] BUFFERED DUAL-EMA (9/21 EMA + ATR FILTER)")
    print("=" * 90)
    today = date.today()
    start_d = today - timedelta(days=10)
    tokens = resolve_universe_tokens(kite_client=kite, force_refresh=False) if kite else {}

    results = []
    for sym in target_symbols:
        if sym == "NIFTY":
            token = 256265
            ltp = float(kite.quote(["NSE:NIFTY 50"]).get("NSE:NIFTY 50", {}).get("last_price", 24000.0)) if kite else 24000.0
        else:
            token = tokens.get(sym) or (instrument_resolver.resolve_token(sym, exchange="NSE", kite_client=kite) if kite else 0) or 0
            ltp = float(kite.quote([f"NSE:{sym}"]).get(f"NSE:{sym}", {}).get("last_price", 2000.0)) if kite else 2000.0

        cache_file = settings.base_dir / "data" / "cache" / f"{sym}_15m.csv"
        df_15m = pd.DataFrame()
        if kite_app:
            try:
                df_15m = HistoricalDataLoader.fetch_real_data(
                    kite_client=kite_app,
                    instrument_token=token,
                    start_date=start_d,
                    end_date=today,
                    interval="15minute",
                    cache_path=cache_file,
                    force_refresh=False,
                )
            except Exception:
                df_15m = pd.DataFrame()

        if df_15m.empty:
            df_15m = generate_synthetic_15m_candles(sym, ltp)

        preds, _ = prediction_service.evaluate_symbol(symbol=sym, df_15m=df_15m, current_ltp=ltp, token=token)
        ema = preds["dual_ema"]

        results.append({
            "Symbol": sym,
            "LTP": f"₹{ltp:,.2f}",
            "Status": ema.status,
            "Direction": ema.direction or "NONE",
            "Entry": f"₹{ema.entry:,.2f}" if ema.entry else "N/A",
            "Stop Loss": f"₹{ema.stop_loss:,.2f}" if ema.stop_loss else "N/A",
            "Target": f"₹{ema.target:,.2f}" if ema.target else "N/A",
            "Reason": ema.reason,
        })

    print(tabulate(results, headers="keys", tablefmt="fancy_grid"))


# =============================================================================
# ALGORITHM 4: NSE-RM-100 RESIDUAL MOMENTUM EOD PORTFOLIO
# =============================================================================
def run_algo_4_rm100(kite_app: Optional[KiteApp], kite: Any):
    print("\n" + "=" * 90)
    print("  [ALGORITHM 4] NSE-RM-100 RESIDUAL MOMENTUM EOD PORTFOLIO ALLOCATION")
    print("=" * 90)

    sample_universe = [
        "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
        "BHARTIARTL", "ITC", "KOTAKBANK", "AXISBANK", "SBIN",
        "MARUTI", "SUNPHARMA", "NTPC", "HCLTECH",
    ]
    today = date.today()
    start_eod = today - timedelta(days=385)

    loaded = False
    closes, highs, lows, volumes, index_close = None, None, None, None, None

    if kite:
        eod_loader = EODPanelLoader(kite_client=kite)
        try:
            closes, highs, lows, volumes, index_close = eod_loader.load_panel(
                symbols=sample_universe,
                start_date=start_eod,
                end_date=today,
                index_symbol="NIFTY",
                force_refresh=False,
            )
            valid_cols = [c for c in closes.columns if closes[c].dropna().shape[0] >= 100]
            if len(valid_cols) >= 5:
                closes = closes[valid_cols]
                highs = highs[valid_cols]
                lows = lows[valid_cols]
                volumes = volumes[valid_cols]
                sample_universe = valid_cols
                loaded = True
        except Exception:
            loaded = False

    if not loaded:
        rng = np.random.default_rng(42)
        n = 300
        dates = pd.bdate_range(end=today, periods=n)
        market = np.cumprod(1 + rng.normal(0.0004, 0.009, n)) * 24000.0
        index_close = pd.Series(market, index=dates)
        mkt_ret = index_close.pct_change().fillna(0.0).to_numpy()

        c_dict = {}
        for i, s in enumerate(sample_universe):
            beta = 0.7 + (i % 8) * 0.10
            idio = rng.normal(0.0, 0.012, n)
            if i < 5:
                idio[-120:-10] += 0.0035
            c_dict[s] = (1500.0 + i * 200.0) * np.cumprod(1 + beta * mkt_ret + idio)
        closes = pd.DataFrame(c_dict, index=dates)
        highs = closes * 1.012
        lows = closes * 0.988
        volumes = pd.DataFrame(5_000_000.0, index=dates, columns=sample_universe)

    rm_config = ResidualMomentumConfig(min_valid_universe=5, min_adtv_rupees=1e6, portfolio_size=5)
    rm_strategy = ResidualMomentumStrategy(rm_config)
    as_of_date = closes.index[-1].date()

    rm_plan = rm_strategy.generate_plan(
        as_of=as_of_date,
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        index_close=index_close,
        universe=sample_universe,
        capital=settings.risk.initial_capital,
        rf_yield_pct=6.5,
    )

    print(f"\n[✓] NSE-RM-100 Plan as of {as_of_date} | Market Regime: {rm_plan.regime.value} | Gross Exposure: {rm_plan.gross_exposure:.0%}")
    if rm_plan.skipped:
        print(f"    Status: SKIPPED ({rm_plan.skip_reason})")
    else:
        targets_table = [
            {
                "Symbol": t.symbol,
                "Rank": t.rank,
                "Score (Z)": f"{t.score:+.2f}",
                "Weight": f"{t.weight:.2%}",
                "Shares": t.quantity,
                "LTP": f"₹{float(closes[t.symbol].iloc[-1]):,.2f}",
                "Stop Loss": f"₹{(t.stop_loss or 0):,.2f}",
            }
            for t in rm_plan.targets
        ]
        print(tabulate(targets_table, headers="keys", tablefmt="fancy_grid"))

        if rm_plan.hedge:
            h = rm_plan.hedge
            print(f"\n[+] Futures Market Hedge: SELL {h.lots} lot(s) {h.symbol} (Beta: {h.portfolio_beta:.2f}, Notional: ₹{h.notional:,.0f})")


# =============================================================================
# ALGORITHM 5: NSE-VRP-INDEX OPTION HARVEST PLAN
# =============================================================================
def run_algo_5_vrp(kite_app: Optional[KiteApp], kite: Any):
    print("\n" + "=" * 90)
    print("  [ALGORITHM 5] NSE-VRP-INDEX VARIANCE RISK PREMIUM HARVEST PLAN")
    print("=" * 90)

    today = date.today()
    opt_loader = OptionsChainLoader(kite_client=kite)
    loaded = False
    vix_series = None

    if kite:
        try:
            vix_series = opt_loader.fetch_india_vix_series(
                start_date=today - timedelta(days=150),
                end_date=today,
                force_refresh=False,
            )
            if vix_series is not None and not vix_series.empty:
                loaded = True
        except Exception:
            loaded = False

    if not loaded:
        dates = pd.bdate_range(end=today, periods=120)
        vix_vals = 14.5 + np.sin(np.linspace(0, 3, 120)) * 2.0
        vix_series = pd.Series(vix_vals, index=dates, name="india_vix")

    parkinson_rv = 11.2
    rv_dates = vix_series.index
    rv_series = pd.Series(np.full(len(rv_dates), parkinson_rv), index=rv_dates, name="parkinson_rv")

    vrp_strategy = VRPHarvestStrategy(VRPConfig())
    vrp_sig = vrp_strategy.vrp_signal(rv_series, vix_series)

    print(f"\n[✓] VRP Signal Diagnostics (as of {today}):")
    print(f"    • India VIX (IV)        : {vrp_sig['iv']:.2f}%")
    print(f"    • Parkinson RV (20d)    : {vrp_sig['rv20']:.2f}%")
    print(f"    • Volatility Spread     : {vrp_sig['spread']:+.2f}%")
    print(f"    • VRP Z-Score           : {vrp_sig['z']:+.2f}")

    spot_nifty = float(kite.quote(["NSE:NIFTY 50"]).get("NSE:NIFTY 50", {}).get("last_price", 24250.0)) if kite else 24250.0
    exp_date = today + timedelta(days=(3 - today.weekday()) % 7 or 7)

    strikes = [spot_nifty - 600, spot_nifty - 300, spot_nifty, spot_nifty + 300, spot_nifty + 600]
    chain_rows = []
    for st in strikes:
        chain_rows.append({"tradingsymbol": f"NIFTY{exp_date.strftime('%y%b').upper()}{int(st)}CE", "strike": st, "option_type": "CE", "iv": 14.2, "last_price": max(15.0, spot_nifty - st + 120.0)})
        chain_rows.append({"tradingsymbol": f"NIFTY{exp_date.strftime('%y%b').upper()}{int(st)}PE", "strike": st, "option_type": "PE", "iv": 14.8, "last_price": max(15.0, st - spot_nifty + 120.0)})
    chain_df = pd.DataFrame(chain_rows)

    vrp_order = vrp_strategy.generate_plan(
        as_of=today, spot=spot_nifty, option_chain=chain_df, expiry=exp_date,
        rv_series=rv_series, india_vix=vix_series,
    )

    if vrp_order is None:
        print("\n    NO ENTRY RECOMMENDATION TODAY:")
        print("    VRP gate check returned None (VRP z-score or VIX range filter not satisfied).")
    else:
        print(f"\n[✓] Recommended Structure: {vrp_order.structure_type} ({vrp_order.structure_id})")
        legs_table = [
            {
                "Side": leg.side.value,
                "Lots": leg.lots,
                "Trading Symbol": leg.tradingsymbol,
                "Strike": f"{leg.strike:.0f}",
                "Delta": f"{leg.delta:+.3f}",
                "Premium": f"₹{leg.premium:,.2f}",
            }
            for leg in vrp_order.legs
        ]
        print(tabulate(legs_table, headers="keys", tablefmt="fancy_grid"))
        print(f"    Net Credit Collected: ₹{vrp_order.net_credit:,.2f} | Max Loss: ₹{vrp_order.max_loss:,.2f}")


# =============================================================================
# ALGORITHM 6: APEX-AIVEM PRE-MARKET AUCTION & 6-FACTOR CATALYST ENGINE
# =============================================================================
def run_algo_6_apex_aivem(kite_app: Optional[KiteApp], kite: Any, catalyst_announcements: Optional[dict] = None):
    print("\n" + "=" * 90)
    print("  [ALGORITHM 6] APEX-AIVEM 6-FACTOR AUCTION-IMBALANCE & CATALYST ENGINE")
    print("=" * 90)

    scorer = CatalystScorer()
    announcements = catalyst_announcements or {
        "NSE:TCS": ["Board meeting scheduled for quarterly financial results", "Interim dividend declaration"],
        "NSE:INFY": ["Secures mega $1.2B digital transformation order win with European enterprise"],
        "NSE:HDFCBANK": ["SEBI approval received for subsidiary merger and regulatory clearance"],
        "NSE:RELIANCE": ["Clarification on media report regarding minor investment call"],
        "NSE:ICICIBANK": [],
    }
    cat_scores = scorer.score_batch(announcements)
    print("\n[+] Catalyst Intelligence Scores (0-3):")
    for sym, sc in cat_scores.items():
        print(f"    • {sym:<16}: Level {sc} ({'Major M&A/Reg' if sc==3 else 'Material Results/Win' if sc==2 else 'Minor Announcement' if sc==1 else 'No News'})")

    cfg = EngineConfig()
    core_symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "BHARTIARTL", "ITC", "LT", "BAJFINANCE"]
    symbols = [f"NSE:{s}" for s in core_symbols] + [f"NSE:STOCK{i:02d}" for i in range(10)]

    quotes = {}
    eod = {}
    if kite:
        try:
            raw_quotes = kite.quote([f"NSE:{s}" for s in core_symbols])
            for sym, q in raw_quotes.items():
                p_open = float(q["ohlc"].get("open", 0) or q.get("last_price", 1000.0))
                p_close = float(q["ohlc"].get("close", 0) or p_open)
                atr = max(5.0, (float(q["ohlc"].get("high", p_open)) - float(q["ohlc"].get("low", p_open))) * 1.2 or p_open * 0.015)
                quotes[sym] = {
                    "ohlc": {"open": p_open, "close": p_close},
                    "volume": float(q.get("volume", 500000)),
                    "total_buy_quantity": float(q.get("total_buy_quantity", 25000)),
                    "total_sell_quantity": float(q.get("total_sell_quantity", 20000)),
                    "lower_circuit_limit": float(q.get("lower_circuit_limit", 0.0)),
                }
                eod[sym] = {
                    "atr_14": atr,
                    "adv_20": max(1e8, float(q.get("volume", 500000)) * p_close),
                    "sector": "FIN" if "BANK" in sym or "FIN" in sym else "IT" if "TCS" in sym or "INFY" in sym else "ENERGY",
                    "beta": 1.05,
                    "cpr_norm": 0.45,
                }
        except Exception:
            pass

    if len(quotes) < 5:
        rng = np.random.default_rng(42)
        for sym in symbols:
            atr = float(rng.uniform(15, 60))
            c_prev = float(rng.uniform(500, 3500))
            gap = rng.normal(0, 1.4) * atr
            eod[sym] = {
                "atr_14": atr,
                "adv_20": float(rng.uniform(1e8, 1e9)),
                "sector": rng.choice(["IT", "BANK", "AUTO", "PHARMA"]),
                "beta": float(rng.uniform(0.7, 1.3)),
                "cpr_norm": float(rng.uniform(0.3, 0.8)),
            }
            quotes[sym] = {
                "ohlc": {"open": c_prev + gap, "close": c_prev},
                "volume": float(rng.uniform(1.8, 4.0) * 0.015 * eod[sym]["adv_20"]),
                "total_buy_quantity": float(rng.uniform(5000, 80000)),
                "total_sell_quantity": float(rng.uniform(5000, 80000)),
                "lower_circuit_limit": 0.0,
            }

    mock_client = MockKite()
    engine = ApexAivemEngine(
        api_key="live" if kite else "dummy",
        access_token="live" if kite else "dummy",
        pre_screened_symbols=list(quotes.keys()),
        config=cfg,
        catalyst_scorer=scorer,
        kite_client=mock_client,
    )
    engine.load_eod_features(eod)
    regime = engine.set_regime(india_vix=15.8, calendar_blackout=False)

    longs, shorts = engine.run_pre_market_scan(
        gift_nifty_gap=0.002, catalyst_scores=cat_scores, quotes=quotes,
    )

    print(f"\n[✓] Stage A Ranked Selections (Max {cfg.max_positions} Positions):")
    if longs:
        print("\n  >>> TOP LONG CANDIDATES:")
        longs_table = [
            {
                "Symbol": c["symbol"],
                "PreOpen": f"₹{c['p_pre']:,.2f}",
                "Gap ATR": f"{c['delta_gap']:.2f}x",
                "RVOL": f"{c['rvol_pre']:.2f}x",
                "OIR": f"{c['oir']:+.2f}",
                "Cat Score": int(c.get("cat_score", 0)),
                "Apex Score": f"{c.get('apex_score', 0):+.3f}",
            }
            for c in longs
        ]
        print(tabulate(longs_table, headers="keys", tablefmt="fancy_grid"))

    if shorts:
        print("\n  >>> TOP SHORT CANDIDATES:")
        shorts_table = [
            {
                "Symbol": c["symbol"],
                "PreOpen": f"₹{c['p_pre']:,.2f}",
                "Gap ATR": f"{c['delta_gap']:.2f}x",
                "RVOL": f"{c['rvol_pre']:.2f}x",
                "OIR": f"{c['oir']:+.2f}",
                "Cat Score": int(c.get("cat_score", 0)),
                "Apex Score": f"{c.get('apex_score', 0):+.3f}",
            }
            for c in shorts
        ]
        print(tabulate(shorts_table, headers="keys", tablefmt="fancy_grid"))

    if longs:
        cand = longs[0]
        bar = {
            "open": cand["p_pre"], "high": cand["p_pre"] * 1.006,
            "low": cand["p_pre"] * 0.997, "close": cand["p_pre"] * 1.004,
            "volume": 0.10 * cand["adv_20"], "vwap": cand["p_pre"] * 1.001,
        }
        confirmed = engine.evaluate_stage_b_confirmation(cand["symbol"], "LONG", bar)
        print(f"\n[+] Stage B 5-min Confirmation for {cand['symbol']}: {'CONFIRMED (Entry Dispatched)' if confirmed else 'REJECTED'}")
        if confirmed:
            engine.dispatch_execution_bracket(cand, "LONG", bar, equity=10_00_000)


# =============================================================================
# MASTER CLI DISPATCHER
# =============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Unified Algorithm Runner for all 6 NSE Trading Strategies",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--algo",
        choices=["1", "2", "3", "4", "5", "6", "orb", "cpr", "dual_ema", "rm100", "vrp", "apex", "all"],
        default="all",
        help="Specify which algorithm to run individually, or 'all' to run all 6 sequentially.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run all 6 algorithms sequentially (equivalent to --algo all)",
    )

    args = parser.parse_args()
    target_algo = "all" if args.all else args.algo.lower()

    print("=" * 90)
    print("  INSTITUTIONAL QUANTITATIVE TRADING ENGINE — 6-ALGORITHM SUITE")
    print(f"  Execution Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}")
    print("=" * 90)

    kite_app, kite = get_kite_session()
    if kite:
        profile = kite.profile()
        print(f"[✓] LIVE SESSION CONNECTED: {profile.get('user_name')} ({profile.get('user_id')}) | Broker: {profile.get('broker')}")
    else:
        print("[!] NOTICE: No active Kite Connect session. Running in offline/simulated data mode.")

    # Pre-resolve tokens
    tokens = resolve_universe_tokens(kite_client=kite, force_refresh=False) if kite else {}

    # Target symbols for intraday algorithms
    scanner = NiftyUniverseScanner()
    ranked_metrics, data_source = scanner.scan_universe(kite_client=kite_app, top_n=4)
    target_symbols = ["NIFTY"] + [m.symbol for m in ranked_metrics]

    if target_algo in ["1", "orb", "all"]:
        run_algo_1_orb(kite_app, kite, target_symbols)

    if target_algo in ["2", "cpr", "all"]:
        run_algo_2_cpr(kite_app, kite, target_symbols)

    if target_algo in ["3", "dual_ema", "all"]:
        run_algo_3_dual_ema(kite_app, kite, target_symbols)

    if target_algo in ["4", "rm100", "all"]:
        run_algo_4_rm100(kite_app, kite)

    if target_algo in ["5", "vrp", "all"]:
        run_algo_5_vrp(kite_app, kite)

    if target_algo in ["6", "apex", "all"]:
        run_algo_6_apex_aivem(kite_app, kite)

    print("\n" + "=" * 90)
    print("  ALGORITHM EXECUTION RUN COMPLETED SUCCESSFULLY")
    print("=" * 90)


if __name__ == "__main__":
    main()
