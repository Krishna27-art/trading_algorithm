"""
Real-time Terminal Dashboard & Telemetry Display for Intraday Algorithmic Engine.
"""

import os
from datetime import datetime
from tabulate import tabulate


class CLIMonitor:
    @staticmethod
    def render_state(
        symbol: str,
        ist_time: datetime,
        phase: str,
        orb_high: float,
        orb_low: float,
        orb_width: float,
        vol_filter_passed: bool,
        vwap: float,
        ltp: float,
        position: int,
        entry_price: float,
        stop_loss: float,
        target: float,
        trailing_active: bool,
        realized_pnl: float,
        unrealized_pnl: float,
        capital: float,
        trades_today: int,
        kill_switch: bool,
    ):
        os.system("cls" if os.name == "nt" else "clear")

        header = [
            ["NSE ALGORITHMIC TRADING HUB", "30-MIN VOLATILITY-FILTERED ORB ENGINE"],
            ["Current IST Time", ist_time.strftime("%Y-%m-%d %H:%M:%S")],
            ["Target Instrument", symbol],
            ["Session Phase", phase],
            ["Kill-Switch Status", "ACTIVE (HALTED)" if kill_switch else "NORMAL (ARMED)"],
        ]

        orb_data = [
            ["ORB High (09:15-09:45)", f"₹{orb_high:,.2f}" if orb_high else "Pending..."],
            ["ORB Low", f"₹{orb_low:,.2f}" if orb_low else "Pending..."],
            ["ORB Width", f"{orb_width:.2f} pts" if orb_width else "Pending..."],
            ["Volatility Cutoff (>= 40 pts)", "PASSED" if vol_filter_passed else "REJECTED" if orb_width else "Evaluating..."],
            ["Session Anchored VWAP", f"₹{vwap:,.2f}" if vwap else "Calculating..."],
            ["Last Traded Price (LTP)", f"₹{ltp:,.2f}" if ltp else "Waiting ticks..."],
        ]

        pos_str = "LONG (+1)" if position == 1 else "SHORT (-1)" if position == -1 else "FLAT (0)"
        pos_data = [
            ["Current Position", pos_str],
            ["Entry Execution Price", f"₹{entry_price:,.2f}" if entry_price else "-"],
            ["Active Stop Loss", f"₹{stop_loss:,.2f}" if stop_loss else "-"],
            ["Profit Target (2.0R)", f"₹{target:,.2f}" if target else "-"],
            ["Breakeven Trailing Active (+1R)", "YES (Risk Eliminated)" if trailing_active else "NO"],
            ["Trades Executed Today", f"{trades_today}/1"],
        ]

        total_pnl = realized_pnl + unrealized_pnl
        pnl_data = [
            ["Account Capital", f"₹{capital:,.2f}"],
            ["Realized P&L", f"₹{realized_pnl:,.2f}"],
            ["Unrealized P&L", f"₹{unrealized_pnl:,.2f}"],
            ["Total Net Day P&L", f"₹{total_pnl:,.2f}"],
            ["Return on Capital", f"{(total_pnl / capital) * 100.0:.2f}%"],
        ]

        print("=" * 75)
        print(tabulate(header, tablefmt="plain"))
        print("-" * 75)
        print("1. MARKET MICROSTRUCTURE & VOLATILITY RANGE")
        print(tabulate(orb_data, headers=["Metric", "Value"], tablefmt="grid"))
        print("\n2. ACTIVE POSITION & RISK CONTROLS")
        print(tabulate(pos_data, headers=["Parameter", "Status"], tablefmt="grid"))
        print("\n3. PORTFOLIO CAPITAL & CIRCUIT-BREAKER")
        print(tabulate(pnl_data, headers=["Metric", "Amount"], tablefmt="grid"))
        print("=" * 75)
