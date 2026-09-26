"""
Command Line Runner for NSE Intraday Strategy Engine.
Supports ORB+VWAP, CPR Regime Breakout, and Buffered Dual-EMA strategies.

Usage:
    python run_algo.py --mode backtest --strategy cpr
    python run_algo.py --mode backtest --strategy dual_ema
    python run_algo.py --mode rolling --strategy cpr
    python run_algo.py --mode scan --top-n 5
    python run_algo.py --mode paper
    python run_algo.py --mode live --broker KITE
"""

import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple
import pandas as pd
from tabulate import tabulate

from backtest.strategy_backtester import StrategyBacktester as EventDrivenBacktester
from backtest.rolling_walk_forward import RollingWalkForwardValidator as WalkForwardValidator
from backtest.rolling_walk_forward import RollingWalkForwardValidator
from config.settings import BrokerType, InstrumentConfig, InstrumentType, settings
from data.historical_loader import HistoricalDataLoader
from data.market_calendar import MarketCalendar
from monitoring.logger import logger
from scanner.stock_ranker import NiftyUniverseScanner, StockRankingMetrics


def run_scanner(top_n: int = 5) -> Tuple[List[StockRankingMetrics], List[InstrumentConfig]]:
    """
    Scans the NIFTY 50 universe using batched live quotes from Kite (if authenticated)
    or synthetic market state, ranking all 50 by explainable breakout/momentum metrics.
    """
    print("\n" + "=" * 85)
    print("  NIFTY 50 UNIVERSE SCANNER: EXPLAINABLE MOMENTUM & BREAKOUT RANKING")
    print("=" * 85)

    kite_app = None
    try:
        from kite_client import KiteApp
        app = KiteApp()
        if app.is_connected():
            kite_app = app
    except Exception:
        pass

    scanner = NiftyUniverseScanner()
    ranked_metrics, data_source = scanner.scan_universe(kite_client=kite_app, top_n=top_n)

    if data_source == "REAL":
        print("[+] CONNECTED: Live Kite Connect Session — batched quotes & real NSE metrics.")
    else:
        print("[!] NOTICE: Kite session offline or unauthenticated. Running in SYNTHETIC DATA MODE.")
        print("    (Run `python auth.py` with valid Kite credentials to scan live NSE market quotes)")

    print("\nScoring Methodology (Transparent 100-pt formula, no black-box models):")
    print("  • RVOL (30 pts max): Institutional volume participation vs 20-day baseline")
    print("  • Gap % (25 pts max): Overnight momentum expansion from previous close")
    print("  • ATR % (25 pts max): Volatility capacity (14-period ATR / close)")
    print("  • VWAP Clearance (20 pts max): Directional expansion distance away from session VWAP")

    table_data = [
        [
            m.rank,
            m.symbol,
            f"₹{m.ltp:,.2f}",
            f"{m.gap_pct:+.2f}%",
            f"{m.rvol:.2f}x",
            f"₹{m.atr_14:.2f}",
            f"{m.atr_pct:.2f}%",
            f"₹{m.vwap:,.2f}",
            f"{m.vwap_dist_pct:+.2f}%",
            f"{m.total_score:.1f}/100",
            m.direction_bias,
        ]
        for m in ranked_metrics
    ]

    headers = [
        "Rank", "Symbol", "LTP", "Gap %", "RVOL", "ATR 14", "ATR %", "VWAP", "VWAP Dist %", "Score", "Bias"
    ]
    print("\n" + tabulate(table_data, headers=headers, tablefmt="fancy_grid"))

    top_configs = scanner.get_top_instrument_configs(ranked_metrics)
    return ranked_metrics, top_configs


def load_history(
    days: int,
    start_date: datetime,
    base_price: float = 24000.0,
    instrument: Optional[InstrumentConfig] = None,
    allow_synthetic: bool = True,
) -> pd.DataFrame:
    """
    Loads historical market data:
    1. Validated local CSV cache
    2. Kite Historical API (if authenticated)
    3. If real data is unavailable:
       - If allow_synthetic=False: raises RuntimeError
       - If allow_synthetic=True: generates synthetic data with clear disclaimer
    """
    target_inst = instrument or settings.instruments[0]
    cache_file = Path("data/cache") / f"{target_inst.symbol}_15m_{start_date.date()}_{days}d.csv"

    # 1. Try validated local cache first
    if cache_file.exists():
        try:
            df, meta = HistoricalDataLoader.load_cached_data_with_validation(cache_file)
            print(f"[+] Loaded {len(df)} validated REAL historical bars from {cache_file} "
                  f"({df['datetime'].dt.date.min()} to {df['datetime'].dt.date.max()}).")
            return df
        except Exception as e:
            logger.warning(f"Cache validation error for {cache_file}: {e}. Attempting live fetch.")

    # 2. Try fetching from Kite Connect Historical API
    if target_inst.instrument_token is not None:
        try:
            from kite_client import KiteApp

            kite_app = KiteApp()
            if kite_app.is_connected():
                df = HistoricalDataLoader.fetch_real_data(
                    kite_client=kite_app,
                    instrument_token=target_inst.instrument_token,
                    start_date=start_date.date(),
                    end_date=start_date.date() + timedelta(days=int(days * 1.5)),
                    interval="15minute",
                    cache_path=cache_file,
                )
                print(f"[+] Downloaded and validated {len(df)} REAL bars from Kite Historical API.")
                return df
            print("[!] Kite session not authenticated (run `python auth.py` first) — "
                  "falling back to synthetic data.")
        except Exception as e:
            logger.warning(f"Live Kite historical fetch failed: {e}")
    else:
        print("[!] No instrument_token set on instrument — checking fallback options.")

    # 3. If real data is not available, check allow_synthetic policy
    if not allow_synthetic:
        raise RuntimeError("REAL HISTORICAL DATA REQUIRED — BACKTEST NOT EXECUTED")

    df = HistoricalDataLoader.generate_synthetic_nifty_data(
        start_date=start_date, days=days, base_price=base_price
    )
    print(f"[!] Loaded {len(df)} SYNTHETIC 15-minute bars "
          f"spanning {df['datetime'].dt.date.nunique()} trading sessions. "
          f"These numbers describe a random walk, not real NIFTY behavior.")
    return df


def build_backtester(strategy_name: str, instrument: InstrumentConfig):
    """
    Maps a --strategy name to a backtester instance.
    'orb'      -> the hardcoded ORB+VWAP EventDrivenBacktester (unchanged).
    'cpr'      -> CPRRegimeBreakoutStrategy through the generic StrategyBacktester.
    'dual_ema' -> BufferedDualEMAStrategy through the generic StrategyBacktester.
    """
    if strategy_name == "orb":
        return EventDrivenBacktester(instrument=instrument, app_settings=settings)
    elif strategy_name == "cpr":
        from backtest.strategy_backtester import StrategyBacktester
        from strategy.cpr_strategy import CPRRegimeBreakoutStrategy
        return StrategyBacktester(
            strategy_factory=lambda: CPRRegimeBreakoutStrategy(instrument, settings.strategy),
            instrument=instrument, app_settings=settings,
        )
    elif strategy_name == "dual_ema":
        from backtest.strategy_backtester import StrategyBacktester
        from strategy.dual_ema_strategy import BufferedDualEMAStrategy
        return StrategyBacktester(
            strategy_factory=lambda: BufferedDualEMAStrategy(instrument, settings.strategy),
            instrument=instrument, app_settings=settings,
        )
    elif strategy_name == "rm100":
        from backtest.rm100_backtest import RM100Backtester
        from strategy.residual_momentum import ResidualMomentumStrategy
        return RM100Backtester(
            strategy=ResidualMomentumStrategy(settings.rm100),
            initial_capital=settings.risk.initial_capital,
        )
    raise ValueError(f"Unknown strategy '{strategy_name}'. Choose from: orb, cpr, dual_ema, rm100, vrp")


def build_backtester_factory(strategy_name: str):
    """Same mapping as build_backtester, but returns a factory of (instrument -> backtester)
    for RollingWalkForwardValidator, which builds a fresh backtester per fold."""
    return lambda instrument: build_backtester(strategy_name, instrument)


def run_backtest(strategy_name: str = "orb", instruments: Optional[List[InstrumentConfig]] = None):
    print("\n" + "=" * 75)
    print(f"  EVENT-DRIVEN BACKTEST — STRATEGY: {strategy_name.upper()}")
    print("=" * 75)

    target_instruments = instruments or [settings.instruments[0]]
    print(f"[+] Running {strategy_name.upper()} Backtester across {len(target_instruments)} instrument(s): "
          f"{', '.join(inst.symbol for inst in target_instruments)}")

    for idx, instrument in enumerate(target_instruments, start=1):
        print(f"\n--- [{idx}/{len(target_instruments)}] BACKTESTING CANDIDATE: {instrument.symbol} ---")
        base_p = 24000.0 if instrument.symbol == "NIFTY" else 2000.0
        df = load_history(days=180, start_date=datetime(2025, 1, 1), base_price=base_p, instrument=instrument)

        backtester = build_backtester(strategy_name, instrument)
        report = backtester.run(df, initial_capital=settings.risk.initial_capital)

        metrics_table = [
            ["Strategy", strategy_name.upper()],
            ["Symbol", instrument.symbol],
            ["Instrument Type", instrument.instrument_type.value],
            ["Total Trades", report.total_trades],
            ["Long Trades", report.long_trades],
            ["Short Trades", report.short_trades],
            ["Winning Trades", f"{report.winning_trades} ({report.win_rate_pct:.1f}%)"],
            ["Losing Trades", f"{report.losing_trades} ({100 - report.win_rate_pct:.1f}%)"],
            ["Gross P&L", f"₹{report.gross_pnl:,.2f}"],
            ["Total Frictions (STT, Brokerage, GST, Stamp, Slip)", f"₹{report.total_transaction_costs:,.2f}"],
            ["Net P&L", f"₹{report.net_pnl:,.2f}"],
            ["Profit Factor", report.profit_factor],
            ["Sharpe Ratio", report.sharpe_ratio],
            ["CAGR", f"{report.cagr_pct:.2f}%"],
            ["Max Strategy Drawdown", f"-{report.max_drawdown_pct:.2f}%"],
            ["Max Consecutive Losses", report.max_consecutive_losses],
            ["Average R / Trade", f"{report.avg_r_multiple:.2f}R"],
            ["Expectancy per Trade", f"₹{report.expectancy_rupees:,.2f}"],
        ]

        print("\n" + tabulate(metrics_table, headers=["Performance Metric", "Value"], tablefmt="fancy_grid"))

    print(f"\n[✓] Backtest Complete for {strategy_name.upper()} across all evaluated instruments. Zero look-ahead bias.")


def run_walk_forward():
    print("\n" + "=" * 75)
    print("  WALK-FORWARD OUT-OF-SAMPLE VALIDATION")
    print("=" * 75)
    df = load_history(days=240, start_date=datetime(2024, 7, 1), base_price=23500.0)
    instrument = settings.instruments[0]
    validator = WalkForwardValidator(instrument=instrument, app_settings=settings)

    print("[+] Running Walk-Forward Split (70% In-Sample / 30% Out-of-Sample)...")
    result = validator.validate(df, split_ratio=0.70, initial_capital=settings.risk.initial_capital)

    comparison = [
        ["Metric", "In-Sample (Training)", "Out-Of-Sample (Testing)"],
        ["Trades", result.in_sample_report.total_trades, result.out_of_sample_report.total_trades],
        ["Win Rate", f"{result.in_sample_report.win_rate_pct:.1f}%", f"{result.out_of_sample_report.win_rate_pct:.1f}%"],
        ["Profit Factor", result.in_sample_report.profit_factor, result.out_of_sample_report.profit_factor],
        ["Net P&L", f"₹{result.in_sample_report.net_pnl:,.2f}", f"₹{result.out_of_sample_report.net_pnl:,.2f}"],
        ["Max Drawdown", f"-{result.in_sample_report.max_drawdown_pct:.2f}%", f"-{result.out_of_sample_report.max_drawdown_pct:.2f}%"],
        ["Sharpe Ratio", result.in_sample_report.sharpe_ratio, result.out_of_sample_report.sharpe_ratio],
    ]

    print("\n" + tabulate(comparison, headers="firstrow", tablefmt="fancy_grid"))
    print(f"\nProfit Factor Retention: {result.profit_factor_retention_pct:.1f}%")
    print(f"Win Rate Retention:       {result.win_rate_retention_pct:.1f}%")
    status_str = "PASSED - STRATEGY IS STATISTICALLY ROBUST" if result.is_statistically_robust else "WARNING - HIGH DEGRADATION"
    print(f"Status:                   {status_str}")


def run_rolling_walk_forward(strategy_name: str = "orb"):
    print("\n" + "=" * 75)
    print(f"  ROLLING WALK-FORWARD SIMULATION — STRATEGY: {strategy_name.upper()} (sequential, no look-ahead)")
    print("=" * 75)
    df = load_history(days=400, start_date=datetime(2023, 6, 1), base_price=23000.0)
    instrument = settings.instruments[0]
    validator = RollingWalkForwardValidator(
        instrument=instrument,
        app_settings=settings,
        backtester_factory=build_backtester_factory(strategy_name),
    )

    print(f"[+] Walking forward with {strategy_name.upper()} in 20-trading-day blocks, each one only ever "
          "seeing data from before it started (60 trading days minimum history "
          "before the first test block)...")
    result = validator.validate(
        df,
        min_train_days=60,
        test_block_days=20,
        initial_capital=settings.risk.initial_capital,
    )

    fold_table = [
        [
            f.fold_number,
            f"{f.test_start} → {f.test_end}",
            f.report.total_trades,
            f"{f.report.win_rate_pct:.1f}%",
            f"₹{f.report.net_pnl:,.2f}",
            f"₹{f.ending_capital:,.2f}",
        ]
        for f in result.folds
    ]
    print("\n" + tabulate(
        fold_table,
        headers=["Fold", "Test Window (unseen at decision time)", "Trades", "Win Rate", "Net P&L", "Capital After"],
        tablefmt="fancy_grid",
    ))

    r = result.combined_out_of_sample_report
    combined_table = [
        ["Total Out-Of-Sample Trades", r.total_trades],
        ["Combined Win Rate", f"{r.win_rate_pct:.1f}%"],
        ["Combined Net P&L", f"₹{r.net_pnl:,.2f}"],
        ["Profit Factor", r.profit_factor],
        ["Sharpe Ratio", r.sharpe_ratio],
        ["Max Drawdown (within OOS series)", f"-{r.max_drawdown_pct:.2f}%"],
        ["Starting Capital", f"₹{settings.risk.initial_capital:,.2f}"],
        ["Final Capital After All Folds", f"₹{result.final_capital:,.2f}"],
    ]
    print("\nCOMBINED OUT-OF-SAMPLE RESULT (this is the real answer):")
    print(tabulate(combined_table, headers=["Metric", "Value"], tablefmt="fancy_grid"))

    if result.fold_statistics:
        fs = result.fold_statistics
        stats_table = [
            ["Median Profit Factor", fs.median_profit_factor],
            ["Median Sharpe Ratio", fs.median_sharpe],
            ["Profitable Folds %", f"{fs.pct_profitable_folds:.1f}%"],
            ["Best Fold Net P&L", f"₹{fs.best_fold_net_pnl:,.2f}"],
            ["Worst Fold Net P&L", f"₹{fs.worst_fold_net_pnl:,.2f}"],
            ["P&L Dispersion StdDev", f"₹{fs.pnl_dispersion_std:,.2f}"],
        ]
        print("\nFOLD AGGREGATE ROBUSTNESS STATISTICS:")
        print(tabulate(stats_table, headers=["Robustness Metric", "Value"], tablefmt="fancy_grid"))

    print("\n[✓] Every fold's test block only used data strictly before it — "
          "no fold ever saw a future candle when deciding a trade.")


def run_paper_simulation(instruments: Optional[List[InstrumentConfig]] = None):
    from broker.paper_broker import PaperBrokerAdapter
    from execution.execution_engine import ExecutionEngine
    from monitoring.cli_monitor import CLIMonitor
    from portfolio.portfolio_manager import PortfolioManager
    from risk.risk_manager import RiskManager

    print("\n" + "=" * 75)
    print("  STARTING LIVE PAPER TRADING ENGINE (MULTI-INSTRUMENT SIMULATION)")
    print("=" * 75)

    target_instruments = instruments or [settings.instruments[0]]
    broker = PaperBrokerAdapter(initial_capital=settings.risk.initial_capital)

    # PORTFOLIO-WIDE RISK GOVERNANCE:
    # A single shared PortfolioManager and RiskManager governs all engines.
    # Enforces 1-trade-per-day max across ALL symbols and 2% daily loss kill-switch.
    shared_portfolio = PortfolioManager(initial_capital=settings.risk.initial_capital)
    shared_risk_manager = RiskManager(
        risk_config=settings.risk,
        max_portfolio_daily_trades=1,
    )

    engines = [
        ExecutionEngine(
            broker=broker,
            instrument=inst,
            app_settings=settings,
            portfolio=shared_portfolio,
            risk_manager=shared_risk_manager,
        )
        for inst in target_instruments
    ]

    for engine in engines:
        engine.start()

    print(f"[+] Active candidate engines: {', '.join(e.instrument.symbol for e in engines)}")
    print("[+] Shared portfolio-wide risk gate initialized (max 1 trade total across all instruments).")

    today = datetime.now().date()
    start_t = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=15)
    end_t = datetime.combine(today, datetime.min.time()).replace(hour=14, minute=35)

    cur_t = start_t
    # Primary instrument for CLI monitor focus
    primary_engine = engines[0]
    cur_price = 24150.0 if primary_engine.instrument.symbol == "NIFTY" else 1500.0

    while cur_t <= end_t:
        price_step = (cur_t - start_t).total_seconds() / 60
        if price_step < 30:  # 09:15 to 09:45
            cur_price += (1.5 if price_step % 2 == 0 else -1.2)
        elif 30 <= price_step < 75:  # Breakout upward
            cur_price += 2.2
        elif price_step >= 75:
            cur_price += 1.8

        # Process ticks across engines
        for engine in engines:
            tick_p = cur_price if engine == primary_engine else (cur_price * 0.98 + (hash(engine.instrument.symbol) % 50))
            engine.process_tick(price=round(tick_p, 2), volume=1200, timestamp=cur_t)

        phase = MarketCalendar.get_session_phase(cur_t.time()).value
        orb = getattr(primary_engine.strategy, "orb", None)
        total_session_trades = sum(shared_risk_manager.daily_trades_count.values())

        CLIMonitor.render_state(
            symbol=primary_engine.instrument.symbol,
            ist_time=cur_t,
            phase=phase,
            orb_high=orb.high if orb else 0.0,
            orb_low=orb.low if orb else 0.0,
            orb_width=orb.width if orb else 0.0,
            vol_filter_passed=orb.is_valid_volatility if orb else False,
            vwap=primary_engine.candle_aggregator.current_vwap,
            ltp=cur_price,
            position=primary_engine.strategy.position,
            entry_price=primary_engine.strategy.entry_price,
            stop_loss=primary_engine.strategy.stop_loss,
            target=primary_engine.strategy.target,
            trailing_active=primary_engine.strategy.trailing_breakeven_active,
            realized_pnl=shared_portfolio.realized_pnl_today,
            unrealized_pnl=shared_portfolio.unrealized_pnl_today,
            capital=shared_portfolio.current_capital,
            trades_today=total_session_trades,
            kill_switch=shared_risk_manager.kill_switch_active,
        )

        cur_t += timedelta(minutes=5)
        time.sleep(0.08)

    for engine in engines:
        engine.stop()

    print("\n[✓] Multi-instrument simulation completed.")


def main():
    parser = argparse.ArgumentParser(description="NSE Intraday Trading Algorithm Engine")
    parser.add_argument(
        "--mode",
        choices=["backtest", "walkforward", "rolling", "paper", "live", "scan"],
        default="backtest",
        help="Operational mode (default: backtest)",
    )
    parser.add_argument(
        "--strategy",
        choices=["orb", "cpr", "dual_ema", "rm100", "vrp"],
        default="orb",
        help="Trading strategy to run/backtest (default: orb)",
    )
    parser.add_argument(
        "--broker",
        choices=["PAPER", "KITE"],
        default="PAPER",
        help="Broker adapter (default: PAPER)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=5,
        help="Number of top-ranked universe candidates to display or trade (default: 5)",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="Target a specific NSE symbol (e.g., RELIANCE) instead of default NIFTY",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Scan the NIFTY 50 universe to pick top candidates before running backtest / paper trading",
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=None,
        help="Path to portfolio data directory for rm100/vrp (default: data/daily)",
    )
    parser.add_argument(
        "--expiry",
        type=str,
        default=None,
        help="Target option expiry date YYYY-MM-DD for vrp",
    )
    args = parser.parse_args()

    # Portfolio-level strategy dispatch
    if args.strategy in ("rm100", "vrp"):
        from run_portfolio import run_rm100, run_vrp
        data_dir = args.data or (settings.base_dir / "data" / "daily" if args.strategy == "rm100" else settings.base_dir / "data" / "vrp")
        if args.strategy == "rm100":
            run_rm100(
                data_dir=data_dir,
                mode="backtest" if args.mode in ("backtest", "rolling", "walkforward") else "plan",
                capital=settings.risk.initial_capital,
                rf=6.5,
            )
        else:
            run_vrp(data_dir=data_dir, expiry=args.expiry)
        return

    if args.mode == "scan":
        run_scanner(top_n=args.top_n)

    elif args.mode == "backtest":
        if args.symbol:
            from config.universe import create_instrument_config_for_equity, resolve_universe_tokens
            tokens = resolve_universe_tokens()
            target_inst = create_instrument_config_for_equity(args.symbol, tokens.get(args.symbol))
            run_backtest(strategy_name=args.strategy, instruments=[target_inst])
        elif args.scan:
            _, top_configs = run_scanner(top_n=args.top_n)
            run_backtest(strategy_name=args.strategy, instruments=top_configs)
        else:
            run_backtest(strategy_name=args.strategy)

    elif args.mode == "walkforward":
        run_walk_forward()

    elif args.mode == "rolling":
        run_rolling_walk_forward(strategy_name=args.strategy)

    elif args.mode == "paper":
        if args.scan:
            _, top_configs = run_scanner(top_n=args.top_n)
            run_paper_simulation(instruments=top_configs)
        else:
            run_paper_simulation()

    elif args.mode == "live":
        print("[!] SAFETY WARNING: Live trading mode requested.")
        if args.broker == "PAPER":
            print("[+] Defaulting to PAPER mode for safety.")
            run_paper_simulation()
        else:
            confirm = input(f"Are you sure you want to trade REAL money with {args.broker}? (type 'CONFIRM'): ")
            if confirm.strip() == "CONFIRM":
                from execution.execution_engine import ExecutionEngine

                print(f"[+] Starting live execution on {args.broker}...")
                if args.broker == "KITE":
                    from broker.kite_adapter import KiteBrokerAdapter
                    broker = KiteBrokerAdapter()
                else:
                    print("[!] Broker not supported.")
                    return
                engine = ExecutionEngine(broker=broker, instrument=settings.instruments[0])
                engine.start()
            else:
                print("[!] Live execution aborted.")


if __name__ == "__main__":
    main()
