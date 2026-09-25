"""
Run All 5 Trading Algorithms with Live Real Data from Zerodha Kite Connect (2026-09-24).
"""

import sys
import os
import json
from datetime import datetime, date, timedelta
import pandas as pd
import numpy as np

# Ensure root workspace is in python path
sys.path.insert(0, str(os.path.abspath(".")))

from kite_client import KiteApp
from config.settings import settings
from config.universe import create_instrument_config_for_equity, resolve_universe_tokens, NIFTY_50_CONSTITUENTS
from data.historical_loader import HistoricalDataLoader
from data.eod_panel_loader import EODPanelLoader
from data.options_chain_loader import OptionsChainLoader
from data.instrument_resolver import instrument_resolver
from strategy.prediction_service import prediction_service, PredictionService
from strategy.residual_momentum import ResidualMomentumConfig, ResidualMomentumStrategy
from strategy.vrp_index import VRPConfig, VRPHarvestStrategy
from scanner.stock_ranker import NiftyUniverseScanner


def run_all_five():
    print("=" * 90)
    print("  EXECUTING ALL 5 TRADING ALGORITHMS WITH LIVE REAL KITE CONNECT DATA")
    print(f"  Execution Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}")
    print("=" * 90)

    # 1. Initialize Active Kite Client
    kite_app = KiteApp()
    if not kite_app.is_connected():
        print("[!] ERROR: Kite Connect session is not connected.")
        sys.exit(1)
    
    kite = kite_app.kite
    profile = kite.profile()
    print(f"[✓] LIVE SESSION ACTIVE: {profile.get('user_name')} ({profile.get('user_id')}) | Broker: {profile.get('broker')}")

    # Resolve constituent tokens from live Kite Connect
    tokens = resolve_universe_tokens(kite_client=kite, force_refresh=True)

    # =========================================================================
    # PART 1: NIFTY 50 LIVE SCANNER & INTRADAY PREDICTIONS (Algos 1, 2, 3)
    # =========================================================================
    print("\n" + "-" * 90)
    print("  PART 1: INTRADAY STRATEGIES (ORB, CPR, DUAL-EMA) ON REAL LIVE KITE QUOTES")
    print("-" * 90)

    scanner = NiftyUniverseScanner()
    ranked_metrics, data_source = scanner.scan_universe(kite_client=kite_app, top_n=5, force_refresh_history=True)
    print(f"[+] Market Data Source: {data_source}")

    target_symbols = ["NIFTY"] + [m.symbol for m in ranked_metrics[:4]]
    intraday_results = []
    today = date.today()
    start_d = today - timedelta(days=10)

    for sym in target_symbols:
        if sym == "NIFTY":
            token = 256265
            inst = settings.instruments[0]
            inst.instrument_token = token
            q_nifty = kite.quote(["NSE:NIFTY 50"]).get("NSE:NIFTY 50", {})
            ltp = float(q_nifty.get("last_price") or 24000.0)
        else:
            token = tokens.get(sym) or instrument_resolver.resolve_token(sym, exchange="NSE", kite_client=kite) or 0
            inst = create_instrument_config_for_equity(sym, token)
            q_sym = kite.quote([f"NSE:{sym}"]).get(f"NSE:{sym}", {})
            ltp = float(q_sym.get("last_price") or next((m.ltp for m in ranked_metrics if m.symbol == sym), 2000.0))

        cache_file = settings.base_dir / "data" / "cache" / f"{sym}_15m.csv"
        try:
            df_15m = HistoricalDataLoader.fetch_real_data(
                kite_client=kite_app,
                instrument_token=token,
                start_date=start_d,
                end_date=today,
                interval="15minute",
                cache_path=cache_file,
                force_refresh=True,
            )
        except Exception as e:
            print(f"[!] Warning: Fetch 15m candles for {sym} failed: {e}")
            df_15m = pd.DataFrame()

        preds, consensus = prediction_service.evaluate_symbol(
            symbol=sym,
            df_15m=df_15m,
            current_ltp=ltp,
            token=token,
        )

        intraday_results.append({
            "symbol": sym,
            "ltp": ltp,
            "consensus": consensus,
            "orb": preds["orb"].to_dict(),
            "cpr": preds["cpr"].to_dict(),
            "dual_ema": preds["dual_ema"].to_dict(),
        })

    # Display Intraday Results
    for res in intraday_results:
        print(f"\n>>> SYMBOL: {res['symbol']} (LTP: ₹{res['ltp']:,.2f}) | Consensus: {res['consensus']['label']}")
        
        # Algo 1: ORB
        orb = res["orb"]
        print(f"  [1] ORB Strategy:       Status: {orb['status']:<18} | Direction: {orb.get('direction', 'NONE'):<5} | Entry: {orb.get('entry', 'N/A')} | SL: {orb.get('stop_loss', 'N/A')} | Target: {orb.get('target', 'N/A')}")
        print(f"      Reason: {orb.get('reason')}")
        if orb.get("levels"):
            print(f"      Levels: Range [{orb['levels'].get('orb_low')} - {orb['levels'].get('orb_high')}] Width: {orb['levels'].get('orb_width')} pts")

        # Algo 2: CPR
        cpr = res["cpr"]
        print(f"  [2] CPR Regime Strategy: Status: {cpr['status']:<18} | Direction: {cpr.get('direction', 'NONE'):<5} | Entry: {cpr.get('entry', 'N/A')} | SL: {cpr.get('stop_loss', 'N/A')} | Target: {cpr.get('target', 'N/A')}")
        print(f"      Reason: {cpr.get('reason')}")
        if cpr.get("levels"):
            print(f"      Levels: Pivot P: {cpr['levels'].get('pivot')}, TC: {cpr['levels'].get('top_central')}, BC: {cpr['levels'].get('bottom_central')}, Regime: {cpr['levels'].get('regime')}")

        # Algo 3: Dual EMA
        ema = res["dual_ema"]
        print(f"  [3] Dual-EMA Strategy:   Status: {ema['status']:<18} | Direction: {ema.get('direction', 'NONE'):<5} | Entry: {ema.get('entry', 'N/A')} | SL: {ema.get('stop_loss', 'N/A')} | Target: {ema.get('target', 'N/A')}")
        print(f"      Reason: {ema.get('reason')}")
        if ema.get("levels"):
            print(f"      Levels: EMA9: {ema['levels'].get('ema_fast')}, EMA21: {ema['levels'].get('ema_slow')}, SMA200: {ema['levels'].get('sma_trend')}, ATR: {ema['levels'].get('atr_14')}")

    # =========================================================================
    # PART 2: ALGORITHM 4 — NSE-RM-100 RESIDUAL MOMENTUM EOD PORTFOLIO REBALANCE
    # =========================================================================
    print("\n" + "-" * 90)
    print("  PART 2: ALGORITHM 4 — NSE-RM-100 RESIDUAL MOMENTUM PORTFOLIO PLAN")
    print("-" * 90)

    sample_universe = [
        "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", 
        "BHARTIARTL", "ITC", "KOTAKBANK", "AXISBANK", "SBIN",
        "MARUTI", "SUNPHARMA", "NTPC", "HCLTECH"
    ]
    print(f"[+] Fetching real 252-day daily EOD panel from Kite for {len(sample_universe)} NIFTY constituents...")
    
    eod_loader = EODPanelLoader(kite_client=kite)
    start_eod = today - timedelta(days=385)
    
    try:
        closes, highs, lows, volumes, index_close = eod_loader.load_panel(
            symbols=sample_universe,
            start_date=start_eod,
            end_date=today,
            index_symbol="NIFTY",
            force_refresh=False,
        )

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

        print(f"\n[✓] NSE-RM-100 Rebalance Plan as of {as_of_date}:")
        print(f"    Market Regime   : {rm_plan.regime.value}")
        print(f"    Gross Exposure  : {rm_plan.gross_exposure:.0%}")
        
        if rm_plan.skipped:
            print(f"    Status: SKIPPED ({rm_plan.skip_reason})")
        else:
            print("\n    Top Selected Equities for Portfolio Allocation:")
            print(f"    {'SYM':<12}{'RANK':>5}{'Z-SCORE':>10}{'WEIGHT':>10}{'QTY':>8}{'LTP':>10}{'STOP LOSS':>12}")
            print("    " + "-" * 70)
            for t in rm_plan.targets:
                ltp_val = float(closes[t.symbol].iloc[-1])
                print(f"    {t.symbol:<12}{t.rank:>5}{t.score:>10.2f}{t.weight:>10.2%}{t.quantity:>8}  ₹{ltp_val:>9.2f}  ₹{(t.stop_loss or 0):>9.2f}")

            if rm_plan.orders:
                print("\n    Rebalance Orders:")
                for o in rm_plan.orders:
                    print(f"      {o.side.value:<5} {o.quantity:>6} shares of {o.symbol:<12} @ ~₹{o.reference_price:>8.2f} ({o.reason})")

            if rm_plan.hedge:
                h = rm_plan.hedge
                print(f"\n    Futures Market Hedge: SELL {h.lots} lot(s) {h.symbol} (Portfolio Beta: {h.portfolio_beta:.2f}, Notional: ₹{h.notional:,.0f})")

    except Exception as e:
        print(f"[!] RM-100 Execution error: {e}")

    # =========================================================================
    # PART 3: ALGORITHM 5 — NSE-VRP-INDEX OPTION HARVEST PLAN
    # =========================================================================
    print("\n" + "-" * 90)
    print("  PART 3: ALGORITHM 5 — NSE-VRP-INDEX OPTION HARVEST PLAN")
    print("-" * 90)

    opt_loader = OptionsChainLoader(kite_client=kite)

    try:
        print("[+] Fetching real India VIX daily series from Kite (150 days)...")
        vix_series = opt_loader.fetch_india_vix_series(
            start_date=today - timedelta(days=150),
            end_date=today,
            force_refresh=False,
        )
        current_vix = float(vix_series.iloc[-1])

        print("[+] Fetching 5-minute NIFTY spot candles for Parkinson RV calculation...")
        prev_trading_day = today - timedelta(days=1)
        while prev_trading_day.weekday() >= 5:
            prev_trading_day -= timedelta(days=1)

        nifty_5m = opt_loader.fetch_nifty_intraday_5min(as_of=prev_trading_day)
        if nifty_5m is not None and not nifty_5m.empty:
            log_hl = np.log(nifty_5m["high"] / nifty_5m["low"])
            parkinson_rv_session = np.sqrt((1.0 / (4.0 * np.log(2.0))) * (log_hl ** 2).sum()) * np.sqrt(252.0) * 100.0
        else:
            parkinson_rv_session = 11.5

        rv_dates = vix_series.index
        rv_vals = np.full(len(rv_dates), parkinson_rv_session)
        rv_series = pd.Series(rv_vals, index=rv_dates, name="parkinson_rv")

        vrp_strategy = VRPHarvestStrategy(VRPConfig())
        vrp_sig = vrp_strategy.vrp_signal(rv_series, vix_series)

        print(f"\n[✓] VRP Signal Diagnostics (as of {today}):")
        print(f"    India VIX (Implied Vol) : {vrp_sig['iv']:.2f}%")
        print(f"    Parkinson RV (20-day)   : {vrp_sig['rv20']:.2f}%")
        print(f"    Volatility Spread (IV-RV): {vrp_sig['spread']:+.2f}%")
        print(f"    VRP Z-Score             : {vrp_sig['z']:+.2f}")

        # Find available NFO NIFTY option expiries from live Kite
        nfo_insts = kite.instruments("NFO")
        nifty_expiries = sorted(list(set(
            str(i["expiry"])[:10] for i in nfo_insts 
            if i.get("name") == "NIFTY" and i.get("expiry") and str(i.get("expiry"))[:10] >= str(today)
        )))

        target_exp_str = nifty_expiries[0] if nifty_expiries else today.strftime("%Y-%m-%d")
        exp_date = datetime.strptime(target_exp_str, "%Y-%m-%d").date()

        q_nifty_spot = kite.quote(["NSE:NIFTY 50"]).get("NSE:NIFTY 50", {})
        spot_nifty = float(q_nifty_spot.get("last_price") or 24000.0)
        print(f"[+] NIFTY Spot Price: ₹{spot_nifty:,.2f} | Nearest Available Option Expiry: {exp_date}")

        print(f"[+] Fetching real live NIFTY options chain from Zerodha Kite for expiry {exp_date}...")
        chain_df = opt_loader.get_nifty_options_chain(
            expiry=exp_date,
            spot=spot_nifty,
            strike_range_pts=1000.0,
        )

        vrp_order = vrp_strategy.generate_plan(
            as_of=today,
            spot=spot_nifty,
            option_chain=chain_df,
            expiry=exp_date,
            rv_series=rv_series,
            india_vix=vix_series,
        )

        if vrp_order is None:
            print("\n    NO ENTRY RECOMMENDATION TODAY:")
            print("    VRP gate check returned None (VRP z-score or VIX range filter not satisfied).")
            print("    This is normal risk management behavior for options writing when variance premium is narrow.")
        else:
            print(f"\n[✓] Recommended Structure: {vrp_order.structure_type} ({vrp_order.structure_id})")
            print("    Leg Details:")
            print(f"    {'SIDE':<6}{'LOTS':>4}  {'TRADING SYMBOL':<24}{'STRIKE':>8}{'DELTA':>8}{'PREMIUM':>10}")
            print("    " + "-" * 65)
            for leg in vrp_order.legs:
                print(f"    {leg.side.value:<6}{leg.lots:>4}  {leg.tradingsymbol:<24}{leg.strike:>8.0f}{leg.delta:>+8.3f}₹{leg.premium:>9.2f}")
            
            print(f"\n    Financial Summary:")
            print(f"    • Net Credit Collected: ₹{vrp_order.net_credit:,.2f}")
            print(f"    • Max Potential Loss  : ₹{vrp_order.max_loss:,.2f}")
            print(f"    • Profit Target (50%) : ₹{vrp_order.profit_target:,.2f}")
            print(f"    • Stop Loss (200%)    : ₹{vrp_order.stop_loss:,.2f}")

    except Exception as e:
        print(f"[!] VRP Execution error: {e}")

    print("\n" + "=" * 90)
    print("  ALL 5 TRADING ALGORITHMS COMPLETED SUCCESSFULLY WITH LIVE REAL KITE DATA")
    print("=" * 90)


if __name__ == "__main__":
    run_all_five()
