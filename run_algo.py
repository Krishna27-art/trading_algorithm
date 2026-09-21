"""
Command Line Runner for NSE Intraday 30-Minute Volatility-Filtered ORB Strategy.
Usage:
    python run_algo.py --mode backtest      # single-pass event-driven backtest
    python run_algo.py --mode walkforward   # one static 70/30 in-sample / out-of-sample split
    python run_algo.py --mode rolling       # sequential day-by-day walk-forward, no look-ahead
    python run_algo.py --mode paper
    python run_algo.py --mode live --broker KITE
"""

import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from tabulate import tabulate

from backtest.event_engine import EventDrivenBacktester
from backtest.walk_forward import WalkForwardValidator
from backtest.rolling_walk_forward import RollingWalkForwardValidator
from config.settings import BrokerType, InstrumentConfig, InstrumentType, settings
from data.historical_loader import HistoricalDataLoader
from data.market_calendar import MarketCalendar
from monitoring.logger import logger

# NOTE: broker/* and execution/execution_engine imports are intentionally
# deferred into run_paper_simulation()/main()'s live branch below, not
# imported here at module level. execution_engine.py pulls in the live tick
# pipeline (data.candle_aggregator, which doesn't exist yet in this repo),
# so importing it unconditionally broke EVERY mode, including plain
# `--mode backtest`, before a single line of backtest code could run.
# Backtesting and paper/live trading are separate concerns; one being
# broken or unfinished shouldn't block the other.


def load_history(days: int, start_date: datetime, base_price: float):
    """
    Real NSE data when a Kite session + instrument_token are available;
    otherwise falls back to the synthetic generator so the CLI still runs,
    with a loud warning so nobody mistakes fake numbers for a real result.
    """
    instrument = settings.instruments[0]
    if instrument.instrument_token is not None:
        try:
            from kite_client import KiteApp  # imported lazily: needs kiteconnect + a valid session

            kite_app = KiteApp()
            if kite_app.is_connected():
                cache_file = Path("data/cache") / (
                    f"{instrument.symbol}_15m_{start_date.date()}_{days}d.csv"
                )
                df = HistoricalDataLoader.fetch_real_data(
                    kite_client=kite_app,
                    instrument_token=instrument.instrument_token,
                    start_date=start_date.date(),
                    end_date=start_date.date() + timedelta(days=int(days * 1.5)),  # pad for weekends/holidays
                    interval="15minute",
                    cache_path=cache_file,
                )
                print(f"[+] Loaded {len(df)} REAL 15-minute bars from Kite Historical API "
                      f"spanning {df['datetime'].dt.date.nunique()} trading sessions.")
                return df
            print("[!] Kite session not authenticated (run `python auth.py` first) — "
                  "falling back to SYNTHETIC data.")
        except Exception as e:
            print(f"[!] Real data fetch failed ({e}) — falling back to SYNTHETIC data.")
    else:
        print("[!] No instrument_token set on settings.instruments[0] — "
              "falling back to SYNTHETIC data. Set instrument_token in config/settings.py "
              "to practice on real NSE history instead.")

    df = HistoricalDataLoader.generate_synthetic_nifty_data(
        start_date=start_date, days=days, base_price=base_price
    )
    print(f"[!] Loaded {len(df)} SYNTHETIC 15-minute bars "
          f"spanning {df['datetime'].dt.date.nunique()} trading sessions. "
          f"These numbers describe a random walk, not real NIFTY behavior.")
    return df


def run_backtest():
    print("\n" + "=" * 75)
    print("  NSE 30-MINUTE VOLATILITY-FILTERED ORB: EVENT-DRIVEN BACKTEST")
    print("=" * 75)
    df = load_history(days=180, start_date=datetime(2025, 1, 1), base_price=24000.0)

    instrument = settings.instruments[0]
    backtester = EventDrivenBacktester(instrument=instrument, app_settings=settings)
    print("[+] Executing realistic backtest with Oct 2024 SEBI statutory friction & slippage...")
    report = backtester.run(df, initial_capital=settings.risk.initial_capital)

    metrics_table = [
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
        ["Long Win Rate vs Short Win Rate", f"{report.long_win_rate:.1f}% vs {report.short_win_rate:.1f}%"],
        ["Long Net P&L vs Short Net P&L", f"₹{report.long_net_pnl:,.2f} vs ₹{report.short_net_pnl:,.2f}"],
    ]

    print("\n" + tabulate(metrics_table, headers=["Performance Metric", "Value"], tablefmt="fancy_grid"))

    if report.yearly_returns:
        yearly_table = [[year, f"{ret:.2f}%"] for year, ret in report.yearly_returns.items()]
        print("\nANNUAL RETURN BREAKDOWN:")
        print(tabulate(yearly_table, headers=["Year", "Net Return (%)"], tablefmt="grid"))

    print("\n[✓] Backtest Complete. Zero look-ahead bias, all trades evaluated strictly on completed candles.")


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


def run_rolling_walk_forward():
    print("\n" + "=" * 75)
    print("  ROLLING WALK-FORWARD SIMULATION (sequential, no look-ahead)")
    print("=" * 75)
    df = load_history(days=400, start_date=datetime(2023, 6, 1), base_price=23000.0)
    instrument = settings.instruments[0]
    validator = RollingWalkForwardValidator(instrument=instrument, app_settings=settings)

    print("[+] Walking forward in 20-trading-day blocks, each one only ever "
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
    print("\n[✓] Every fold's test block only used data strictly before it — "
          "no fold ever saw a future candle when deciding a trade.")


def run_paper_simulation():
    from broker.paper_broker import PaperBrokerAdapter
    from execution.execution_engine import ExecutionEngine
    from monitoring.cli_monitor import CLIMonitor

    print("\n" + "=" * 75)
    print("  STARTING LIVE PAPER TRADING ENGINE (SIMULATION)")
    print("=" * 75)
    broker = PaperBrokerAdapter(initial_capital=settings.risk.initial_capital)
    instrument = settings.instruments[0]
    engine = ExecutionEngine(broker=broker, instrument=instrument, app_settings=settings)
    engine.start()

    print("[+] Generating simulated live trading day ticks (09:15 to 14:35 IST)...")
    today = datetime.now().date()
    start_t = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=15)
    end_t = datetime.combine(today, datetime.min.time()).replace(hour=14, minute=35)

    cur_t = start_t
    cur_price = 24150.0

    while cur_t <= end_t:
        # Generate simulated 1-minute tick update
        price_step = (cur_t - start_t).total_seconds() / 60
        # Create a morning breakout pattern
        if price_step < 30: # 09:15 to 09:45
            cur_price += (1.5 if price_step % 2 == 0 else -1.2)
        elif 30 <= price_step < 75: # Breakout upward
            cur_price += 2.2
        elif price_step >= 75: # Target reach
            cur_price += 1.8

        engine.process_tick(price=round(cur_price, 2), volume=1200, timestamp=cur_t)

        phase = MarketCalendar.get_session_phase(cur_t.time()).value
        orb = engine.strategy.orb
        CLIMonitor.render_state(
            symbol=instrument.symbol,
            ist_time=cur_t,
            phase=phase,
            orb_high=orb.high if orb else 0.0,
            orb_low=orb.low if orb else 0.0,
            orb_width=orb.width if orb else 0.0,
            vol_filter_passed=orb.is_valid_volatility if orb else False,
            vwap=engine.candle_aggregator.current_vwap,
            ltp=cur_price,
            position=engine.strategy.position,
            entry_price=engine.strategy.entry_price,
            stop_loss=engine.strategy.stop_loss,
            target=engine.strategy.target,
            trailing_active=engine.strategy.trailing_breakeven_active,
            realized_pnl=engine.portfolio.realized_pnl_today,
            unrealized_pnl=engine.portfolio.unrealized_pnl_today,
            capital=engine.portfolio.current_capital,
            trades_today=engine.strategy.trades_today,
            kill_switch=engine.risk_manager.kill_switch_active,
        )

        cur_t += timedelta(minutes=5)
        time.sleep(0.1) # Fast-forward simulation

    engine.stop()
    print("\n[✓] Simulation completed. Trade logged in SQLite database.")


def main():
    parser = argparse.ArgumentParser(description="NSE Intraday 30-Minute Volatility-Filtered ORB Strategy Engine")
    parser.add_argument(
        "--mode",
        choices=["backtest", "walkforward", "rolling", "paper", "live"],
        default="backtest",
        help="Operational mode (default: backtest)",
    )
    parser.add_argument(
        "--broker",
        choices=["PAPER", "KITE", "DHAN"],
        default="PAPER",
        help="Broker adapter (default: PAPER)",
    )
    args = parser.parse_args()

    if args.mode == "backtest":
        run_backtest()
    elif args.mode == "walkforward":
        run_walk_forward()
    elif args.mode == "rolling":
        run_rolling_walk_forward()
    elif args.mode == "paper":
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
                    from broker.dhan_adapter import DhanBrokerAdapter
                    broker = DhanBrokerAdapter()
                engine = ExecutionEngine(broker=broker, instrument=settings.instruments[0])
                engine.start()
            else:
                print("[!] Live execution aborted.")


if __name__ == "__main__":
    main()
