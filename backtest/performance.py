"""
Institutional Backtesting Performance Analytics.
Calculates CAGR, Sharpe, Drawdown, Profit Factor, Expectancy, R-metrics, and cost impact.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional
import numpy as np
import pandas as pd


@dataclass
class PerformanceReport:
    total_trades: int
    long_trades: int
    short_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    gross_pnl: float
    net_pnl: float
    total_transaction_costs: float
    cost_drag_pct: float
    profit_factor: float
    sharpe_ratio: float
    cagr_pct: float
    max_drawdown_pct: float
    max_consecutive_losses: int
    avg_r_multiple: float
    expectancy_rupees: float
    long_win_rate: float
    short_win_rate: float
    long_net_pnl: float
    short_net_pnl: float
    yearly_returns: Dict[int, float]
    monthly_returns: Dict[str, float]


class PerformanceAnalyzer:
    @staticmethod
    def generate_report(
        trades: List[dict],
        initial_capital: float = 1000000.0,
        risk_free_rate: float = 0.065,  # 6.5% Indian 10-yr G-sec
        backtest_start_date: Optional[datetime] = None,
        backtest_end_date: Optional[datetime] = None,
        all_trading_dates: Optional[List] = None,
    ) -> PerformanceReport:
        if not trades:
            return PerformanceReport(
                total_trades=0, long_trades=0, short_trades=0, winning_trades=0, losing_trades=0,
                win_rate_pct=0.0, gross_pnl=0.0, net_pnl=0.0, total_transaction_costs=0.0,
                cost_drag_pct=0.0, profit_factor=0.0, sharpe_ratio=0.0, cagr_pct=0.0,
                max_drawdown_pct=0.0, max_consecutive_losses=0, avg_r_multiple=0.0,
                expectancy_rupees=0.0, long_win_rate=0.0, short_win_rate=0.0,
                long_net_pnl=0.0, short_net_pnl=0.0, yearly_returns={}, monthly_returns={}
            )

        df = pd.DataFrame(trades)
        df["entry_time"] = pd.to_datetime(df["entry_time"])
        df.sort_values("entry_time", inplace=True)

        total_trades = len(df)
        long_df = df[df["direction"] == "BUY"]
        short_df = df[df["direction"] == "SELL"]

        winning_df = df[df["pnl_net"] > 0]
        losing_df = df[df["pnl_net"] <= 0]

        win_count = len(winning_df)
        loss_count = len(losing_df)
        win_rate = (win_count / total_trades) * 100.0 if total_trades > 0 else 0.0

        gross_pnl = float(df["pnl_gross"].sum())
        net_pnl = float(df["pnl_net"].sum())
        total_costs = float(df["total_costs"].sum())
        cost_drag = (total_costs / gross_pnl * 100.0) if gross_pnl > 0 else (100.0 if total_costs > 0 else 0.0)

        gross_wins = float(winning_df["pnl_net"].sum()) if not winning_df.empty else 0.0
        gross_losses = abs(float(losing_df["pnl_net"].sum())) if not losing_df.empty else 0.0
        if gross_losses > 0:
            profit_factor = round(gross_wins / gross_losses, 2)
        elif gross_wins > 0:
            profit_factor = float("inf")
        else:
            profit_factor = 0.0

        # Equity Curve and Drawdowns (trade-by-trade realized equity)
        equity = initial_capital + df["pnl_net"].cumsum()
        peak = np.maximum.accumulate(equity)
        drawdown = (equity - peak) / peak
        max_drawdown_pct = abs(float(drawdown.min())) * 100.0

        # Duration & CAGR (using requested backtest duration if provided)
        start_date = pd.to_datetime(backtest_start_date) if backtest_start_date is not None else df["entry_time"].iloc[0]
        end_date = pd.to_datetime(backtest_end_date) if backtest_end_date is not None else df["entry_time"].iloc[-1]
        days = max((end_date - start_date).days, 1)
        years = days / 365.25
        ending_capital = initial_capital + net_pnl
        if ending_capital > 0 and years > 0:
            cagr = ((ending_capital / initial_capital) ** (1.0 / years) - 1.0) * 100.0
        else:
            cagr = 0.0

        # Daily Returns & Sharpe Ratio
        # Incorporates full trading days (including zero-return days) to prevent distortion
        df["date"] = df["entry_time"].dt.date
        trade_daily_pnl = df.groupby("date")["pnl_net"].sum()

        if all_trading_dates is not None and len(all_trading_dates) > 0:
            # Full calendar/trading day series
            daily_series = pd.Series(0.0, index=all_trading_dates)
            for d, val in trade_daily_pnl.items():
                if d in daily_series.index:
                    daily_series[d] = val
            daily_returns = daily_series / initial_capital
        else:
            daily_returns = trade_daily_pnl / initial_capital

        mean_ret = daily_returns.mean()
        std_ret = daily_returns.std()
        if std_ret > 0 and len(daily_returns) > 1:
            # Annualize with 252 trading days
            excess_return = (mean_ret * 252) - risk_free_rate
            sharpe = excess_return / (std_ret * np.sqrt(252))
        else:
            sharpe = 0.0

        # Consecutive Losses
        consec_losses = 0
        max_consec_losses = 0
        for pnl in df["pnl_net"]:
            if pnl <= 0:
                consec_losses += 1
                max_consec_losses = max(max_consec_losses, consec_losses)
            else:
                consec_losses = 0

        # Average R & Expectancy
        avg_r = float(df["r_multiple"].mean()) if "r_multiple" in df.columns else 0.0
        expectancy = net_pnl / total_trades if total_trades > 0 else 0.0

        # Directional Asymmetry Breakdown
        long_win_count = len(long_df[long_df["pnl_net"] > 0])
        long_win_rate = (long_win_count / len(long_df) * 100.0) if len(long_df) > 0 else 0.0
        long_net = float(long_df["pnl_net"].sum()) if not long_df.empty else 0.0

        short_win_count = len(short_df[short_df["pnl_net"] > 0])
        short_win_rate = (short_win_count / len(short_df) * 100.0) if len(short_df) > 0 else 0.0
        short_net = float(short_df["pnl_net"].sum()) if not short_df.empty else 0.0

        # Yearly & Monthly Returns
        df["year"] = df["entry_time"].dt.year
        yearly = (df.groupby("year")["pnl_net"].sum() / initial_capital * 100.0).to_dict()

        df["year_month"] = df["entry_time"].dt.strftime("%Y-%m")
        monthly = (df.groupby("year_month")["pnl_net"].sum() / initial_capital * 100.0).to_dict()

        return PerformanceReport(
            total_trades=total_trades,
            long_trades=len(long_df),
            short_trades=len(short_df),
            winning_trades=win_count,
            losing_trades=loss_count,
            win_rate_pct=round(win_rate, 2),
            gross_pnl=round(gross_pnl, 2),
            net_pnl=round(net_pnl, 2),
            total_transaction_costs=round(total_costs, 2),
            cost_drag_pct=round(cost_drag, 2),
            profit_factor=round(profit_factor, 2),
            sharpe_ratio=round(float(sharpe), 2),
            cagr_pct=round(cagr, 2),
            max_drawdown_pct=round(max_drawdown_pct, 2),
            max_consecutive_losses=max_consec_losses,
            avg_r_multiple=round(avg_r, 2),
            expectancy_rupees=round(expectancy, 2),
            long_win_rate=round(long_win_rate, 2),
            short_win_rate=round(short_win_rate, 2),
            long_net_pnl=round(long_net, 2),
            short_net_pnl=round(short_net, 2),
            yearly_returns={k: round(v, 2) for k, v in yearly.items()},
            monthly_returns={k: round(v, 2) for k, v in monthly.items()},
        )
