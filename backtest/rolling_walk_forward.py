"""
Rolling (Anchored) Walk-Forward Simulation.

This is the piece that actually answers "how would the algorithm have
worked, day by day, the day before that, the day before that... without
ever knowing what happens next?"

walk_forward.py (the existing file) does ONE static split: the first 70% of
days are "in-sample", the last 30% are "out-of-sample". That's a fine sanity
check, but it only proves the strategy holds up on one single unseen block
at the end of history. It doesn't tell you whether it would have kept
working consistently across many different stretches of time.

This engine instead walks forward through the whole history in sequential
blocks:

    fold 1:  train on days [0 .. 60)   -> test on days [60 .. 80)
    fold 2:  train on days [0 .. 80)   -> test on days [80 .. 100)
    fold 3:  train on days [0 .. 100)  -> test on days [100 .. 120)
    ...

Each fold's test window is strictly *after* everything it was "trained" on
(the train window only anchors/expands forward — it never includes a single
bar from the test window or beyond). Account capital carries over fold to
fold, so the concatenated out-of-sample results form one continuous equity
curve — literally what you'd have ended up with running this live, one real
trading day after another, never seeing tomorrow's candle before today's
decisions were made.

The strategy here is rule-based (fixed ORB/VWAP logic), so by default
"training" a fold does nothing but define the window — the same instrument
config is used everywhere. If you want genuine walk-forward *optimization*
(re-picking min_orb_range / risk_reward_ratio per fold from only the
trailing window), pass a `param_selector` callback; it receives the
training slice and must return an InstrumentConfig, which is then used for
that fold's out-of-sample test only.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, List, Optional, Tuple

import pandas as pd

from backtest.event_engine import EventDrivenBacktester
from backtest.performance import PerformanceAnalyzer, PerformanceReport
from config.settings import AppSettings, InstrumentConfig, settings
from monitoring.logger import logger

ParamSelector = Callable[[pd.DataFrame], InstrumentConfig]


@dataclass
class Fold:
    fold_number: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    report: PerformanceReport
    starting_capital: float
    ending_capital: float


@dataclass
class RollingWalkForwardResult:
    folds: List[Fold]
    combined_out_of_sample_report: PerformanceReport
    final_capital: float
    all_out_of_sample_trades: List[dict] = field(default_factory=list)


class RollingWalkForwardValidator:
    def __init__(self, instrument: InstrumentConfig, app_settings: AppSettings = settings):
        self.instrument = instrument
        self.settings = app_settings

    def validate(
        self,
        df_15m: pd.DataFrame,
        min_train_days: int = 60,
        test_block_days: int = 20,
        initial_capital: float = 1_000_000.0,
        param_selector: Optional[ParamSelector] = None,
    ) -> RollingWalkForwardResult:
        data = df_15m.copy()
        if "datetime" not in data.columns and isinstance(data.index, pd.DatetimeIndex):
            data["datetime"] = data.index
        data["datetime"] = pd.to_datetime(data["datetime"])
        data.sort_values("datetime", inplace=True)
        data["date"] = data["datetime"].dt.date

        trading_days = sorted(data["date"].unique())
        if len(trading_days) < min_train_days + test_block_days:
            raise ValueError(
                f"Only {len(trading_days)} trading days available — need at least "
                f"{min_train_days + test_block_days} (min_train_days + test_block_days) "
                f"to run even one fold. Fetch a longer history or lower these thresholds."
            )

        folds: List[Fold] = []
        all_oos_trades: List[dict] = []
        running_capital = initial_capital

        cursor = min_train_days
        fold_number = 0
        while cursor < len(trading_days):
            fold_number += 1
            test_dates = trading_days[cursor: cursor + test_block_days]
            if not test_dates:
                break
            train_dates = trading_days[:cursor]  # anchored/expanding — everything strictly before the test block

            df_train = data[data["date"].isin(train_dates)]
            df_test = data[data["date"].isin(test_dates)]

            instrument_for_fold = self.instrument
            if param_selector is not None:
                try:
                    instrument_for_fold = param_selector(df_train)
                except Exception as e:
                    logger.error(f"param_selector failed on fold {fold_number}, "
                                 f"falling back to base instrument config: {e}")

            backtester = EventDrivenBacktester(instrument_for_fold, self.settings)
            starting_capital = running_capital

            logger.info(
                f"Fold {fold_number}: train=[{train_dates[0]}..{train_dates[-1]}] "
                f"({len(train_dates)} days) -> test=[{test_dates[0]}..{test_dates[-1]}] "
                f"({len(test_dates)} days), starting capital ₹{starting_capital:,.2f}"
            )

            fold_trades = backtester.generate_trades(df_test, initial_capital=starting_capital)
            report = PerformanceAnalyzer.generate_report(fold_trades, initial_capital=starting_capital)
            running_capital = starting_capital + report.net_pnl
            all_oos_trades.extend(fold_trades)

            folds.append(Fold(
                fold_number=fold_number,
                train_start=train_dates[0],
                train_end=train_dates[-1],
                test_start=test_dates[0],
                test_end=test_dates[-1],
                report=report,
                starting_capital=starting_capital,
                ending_capital=running_capital,
            ))

            cursor += test_block_days

        combined_report = PerformanceAnalyzer.generate_report(
            all_oos_trades, initial_capital=initial_capital
        )

        return RollingWalkForwardResult(
            folds=folds,
            combined_out_of_sample_report=combined_report,
            final_capital=running_capital,
            all_out_of_sample_trades=all_oos_trades,
        )
