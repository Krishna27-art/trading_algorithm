"""
Static In-Sample / Out-Of-Sample Walk Forward Validator (70/30 Split).
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional
import pandas as pd

from backtest.event_engine import EventDrivenBacktester
from backtest.performance import PerformanceAnalyzer, PerformanceReport
from config.settings import AppSettings, InstrumentConfig, settings


@dataclass
class WalkForwardResult:
    in_sample_report: PerformanceReport
    out_of_sample_report: PerformanceReport
    profit_factor_retention_pct: float
    win_rate_retention_pct: float
    is_statistically_robust: bool


class WalkForwardValidator:
    def __init__(self, instrument: InstrumentConfig, app_settings: AppSettings = settings):
        self.instrument = instrument
        self.settings = app_settings

    def validate(
        self,
        df_15m: pd.DataFrame,
        split_ratio: float = 0.70,
        initial_capital: float = 1000000.0,
    ) -> WalkForwardResult:
        data = df_15m.copy()
        if "datetime" not in data.columns and isinstance(data.index, pd.DatetimeIndex):
            data = data.reset_index()
            data.rename(columns={"index": "datetime"}, inplace=True)

        data["datetime"] = pd.to_datetime(data["datetime"])
        unique_dates = sorted(data["datetime"].dt.date.unique())
        split_idx = int(len(unique_dates) * split_ratio)

        in_sample_dates = set(unique_dates[:split_idx])
        out_of_sample_dates = set(unique_dates[split_idx:])

        df_in = data[data["datetime"].dt.date.isin(in_sample_dates)].copy()
        df_out = data[data["datetime"].dt.date.isin(out_of_sample_dates)].copy()

        backtester = EventDrivenBacktester(instrument=self.instrument, app_settings=self.settings)

        in_trades = backtester.generate_trades(df_in, initial_capital=initial_capital)
        in_report = PerformanceAnalyzer.generate_report(in_trades, initial_capital=initial_capital)

        out_trades = backtester.generate_trades(df_out, initial_capital=initial_capital)
        out_report = PerformanceAnalyzer.generate_report(out_trades, initial_capital=initial_capital)

        # Retention calculations
        pf_in = in_report.profit_factor if in_report.profit_factor != float("inf") else 2.0
        pf_out = out_report.profit_factor if out_report.profit_factor != float("inf") else 2.0
        pf_retention = (pf_out / pf_in * 100.0) if pf_in > 0 else 0.0

        wr_in = in_report.win_rate_pct
        wr_out = out_report.win_rate_pct
        wr_retention = (wr_out / wr_in * 100.0) if wr_in > 0 else 0.0

        is_robust = pf_retention >= 50.0 and out_report.net_pnl > 0

        return WalkForwardResult(
            in_sample_report=in_report,
            out_of_sample_report=out_report,
            profit_factor_retention_pct=round(pf_retention, 2),
            win_rate_retention_pct=round(wr_retention, 2),
            is_statistically_robust=is_robust,
        )
