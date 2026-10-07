"""
Static In-Sample / Out-Of-Sample Walk Forward Validator (70/30 Split).
Now strategy-aware: pass strategy_name="cpr" / "dual_ema" / "apex" to
validate any registered strategy (not just ORB).
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional
import pandas as pd

from backend.backtest.strategy_backtester import EventDrivenBacktester
from backend.backtest.performance import PerformanceAnalyzer, PerformanceReport
from backend.config.settings import AppSettings, InstrumentConfig, settings


@dataclass
class WalkForwardResult:
    in_sample_report: PerformanceReport
    out_of_sample_report: PerformanceReport
    profit_factor_retention_pct: float
    win_rate_retention_pct: float
    is_statistically_robust: bool


class WalkForwardValidator:
    def __init__(
        self,
        instrument: InstrumentConfig,
        app_settings: AppSettings = settings,
        strategy_name: str = "orb",
    ):
        """
        strategy_name: 'orb' | 'cpr' | 'dual_ema' | 'apex'
        Selects which backtester class to instantiate for in-sample and
        out-of-sample periods. Defaults to 'orb' for backwards compatibility.
        """
        self.instrument = instrument
        self.settings = app_settings
        self.strategy_name = strategy_name

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

        # Fix 3.8: Build the correct backtester based on strategy_name
        from backend.backtest.strategy_backtester import StrategyBacktester
        if self.strategy_name == "orb":
            bt_in = EventDrivenBacktester(instrument=self.instrument, app_settings=self.settings)
            bt_out = EventDrivenBacktester(instrument=self.instrument, app_settings=self.settings)
        elif self.strategy_name == "cpr":
            from backend.strategy.cpr_strategy import CPRRegimeBreakoutStrategy
            bt_in = StrategyBacktester(
                strategy_factory=lambda: CPRRegimeBreakoutStrategy(self.instrument, self.settings.strategy),
                instrument=self.instrument, app_settings=self.settings, persist_trades=False,
            )
            bt_out = StrategyBacktester(
                strategy_factory=lambda: CPRRegimeBreakoutStrategy(self.instrument, self.settings.strategy),
                instrument=self.instrument, app_settings=self.settings, persist_trades=False,
            )
        elif self.strategy_name == "dual_ema":
            from backend.strategy.dual_ema_strategy import BufferedDualEMAStrategy
            bt_in = StrategyBacktester(
                strategy_factory=lambda: BufferedDualEMAStrategy(self.instrument, self.settings.strategy),
                instrument=self.instrument, app_settings=self.settings, persist_trades=False,
            )
            bt_out = StrategyBacktester(
                strategy_factory=lambda: BufferedDualEMAStrategy(self.instrument, self.settings.strategy),
                instrument=self.instrument, app_settings=self.settings, persist_trades=False,
            )
        elif self.strategy_name == "apex":
            from backend.strategy.apex_engine import ApexStrategy
            bt_in = StrategyBacktester(
                strategy_factory=lambda: ApexStrategy(self.instrument, self.settings.strategy),
                instrument=self.instrument, app_settings=self.settings, persist_trades=False,
            )
            bt_out = StrategyBacktester(
                strategy_factory=lambda: ApexStrategy(self.instrument, self.settings.strategy),
                instrument=self.instrument, app_settings=self.settings, persist_trades=False,
            )
        else:
            bt_in = EventDrivenBacktester(instrument=self.instrument, app_settings=self.settings)
            bt_out = EventDrivenBacktester(instrument=self.instrument, app_settings=self.settings)

        # Out-of-sample gets the in-sample data as pretrain context (no look-ahead)
        in_trades = bt_in.generate_trades(df_in, initial_capital=initial_capital)
        in_report = PerformanceAnalyzer.generate_report(in_trades, initial_capital=initial_capital)

        out_trades = bt_out.generate_trades(
            df_out, initial_capital=initial_capital, pretrain_df=df_in
        )
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
