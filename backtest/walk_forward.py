"""
Walk-Forward Out-Of-Sample Validation Engine.
Splits data into In-Sample (Training) and Out-Of-Sample (Testing) windows to prevent overfitting.
"""

from dataclasses import dataclass
from typing import Tuple
import pandas as pd

from backtest.event_engine import EventDrivenBacktester
from backtest.performance import PerformanceReport
from config.settings import AppSettings, InstrumentConfig, settings
from monitoring.logger import logger


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
        self.backtester = EventDrivenBacktester(instrument, app_settings)

    def validate(
        self,
        df_15m: pd.DataFrame,
        split_ratio: float = 0.70,
        initial_capital: float = 1000000.0,
    ) -> WalkForwardResult:
        """
        Runs Walk-Forward analysis:
        - 70% In-Sample
        - 30% Out-Of-Sample
        """
        data = df_15m.copy()
        if "datetime" not in data.columns and isinstance(data.index, pd.DatetimeIndex):
            data["datetime"] = data.index
        data["datetime"] = pd.to_datetime(data["datetime"])
        data.sort_values("datetime", inplace=True)

        dates = sorted(data["datetime"].dt.date.unique())
        split_idx = int(len(dates) * split_ratio)

        in_sample_dates = set(dates[:split_idx])
        out_of_sample_dates = set(dates[split_idx:])

        df_in = data[data["datetime"].dt.date.isin(in_sample_dates)]
        df_out = data[data["datetime"].dt.date.isin(out_of_sample_dates)]

        logger.info(f"Running In-Sample Backtest ({len(in_sample_dates)} trading sessions)...")
        in_sample_report = self.backtester.run(df_in, initial_capital=initial_capital)

        logger.info(f"Running Out-Of-Sample Backtest ({len(out_of_sample_dates)} trading sessions)...")
        out_of_sample_report = self.backtester.run(df_out, initial_capital=initial_capital)

        # Retention metrics
        pf_in = max(in_sample_report.profit_factor, 0.01)
        pf_out = out_of_sample_report.profit_factor
        pf_retention = (pf_out / pf_in) * 100.0

        wr_in = max(in_sample_report.win_rate_pct, 0.01)
        wr_out = out_of_sample_report.win_rate_pct
        wr_retention = (wr_out / wr_in) * 100.0

        # Research standard: Out-of-sample profit factor should remain > 1.10 and win rate > 40%
        is_robust = pf_out >= 1.10 and wr_out >= 40.0 and out_of_sample_report.net_pnl > 0

        return WalkForwardResult(
            in_sample_report=in_sample_report,
            out_of_sample_report=out_of_sample_report,
            profit_factor_retention_pct=round(pf_retention, 2),
            win_rate_retention_pct=round(wr_retention, 2),
            is_statistically_robust=is_robust,
        )
