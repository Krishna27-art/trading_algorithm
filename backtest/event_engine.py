"""
Event-Driven Backtest Engine for Intraday Strategies (with ORB+VWAP and Strategy Support).
Includes execution policies, statutory costs, and zero look-ahead bias validation.
"""

from enum import Enum
from typing import Callable, List, Optional
import pandas as pd
from datetime import datetime, time

from backtest.performance import PerformanceAnalyzer, PerformanceReport
from backtest.strategy_backtester import StrategyBacktester
from config.settings import AppSettings, InstrumentConfig, settings
from strategy.base_strategy import BaseStrategy
from strategy.orb_strategy import IntradayORBStrategy


class ExecutionPolicy(str, Enum):
    CONSERVATIVE = "CONSERVATIVE"
    OPTIMISTIC = "OPTIMISTIC"
    REALISTIC = "REALISTIC"


class EventDrivenBacktester(StrategyBacktester):
    def __init__(
        self,
        instrument: InstrumentConfig,
        app_settings: AppSettings = settings,
        execution_policy: ExecutionPolicy = ExecutionPolicy.CONSERVATIVE,
        strategy_factory: Optional[Callable[[], BaseStrategy]] = None,
        context_lookback_days: int = 30,
        slippage_points: float = 0.5,
    ):
        factory = strategy_factory or (
            lambda: IntradayORBStrategy(
                instrument=instrument,
                strategy_config=app_settings.strategy,
                execution_policy=execution_policy,
            )
        )
        super().__init__(
            strategy_factory=factory,
            instrument=instrument,
            app_settings=app_settings,
            context_lookback_days=context_lookback_days,
        )
        self.execution_policy = execution_policy
        self.slippage_points = slippage_points

