from datetime import datetime

import numpy as np
import pandas as pd

from backend.config.settings import InstrumentConfig, StrategyConfig
from backend.strategy.crsd_strategy import CRSDStrategy
from backend.strategy.prediction_service import PredictionService
from backend.strategy.sector_impulse_strategy import (
    SectorImpulseStrategy,
    _session_rolling_sum,
)


def test_sector_impulse_rolling_windows_do_not_cross_session_boundaries():
    index = pd.to_datetime(
        [
            "2025-01-06 09:30:00",
            "2025-01-06 09:45:00",
            "2025-01-07 09:30:00",
            "2025-01-07 09:45:00",
        ]
    )
    values = pd.Series([1.0, 2.0, 100.0, 4.0], index=index)

    result = _session_rolling_sum(values, window=2)

    assert np.isnan(result.iloc[0])
    assert result.iloc[1] == 3.0
    # First return of a new session has no within-session two-bar window.
    assert np.isnan(result.iloc[2])
    assert result.iloc[3] == 104.0


def test_sector_impulse_converts_candle_open_to_completion_timestamp():
    instrument = InstrumentConfig(symbol="TEST", exchange="NSE")
    strategy = SectorImpulseStrategy(
        instrument,
        StrategyConfig(candle_timeframe_minutes=15),
        ctx=None,
    )

    assert strategy._event_timestamp(datetime(2025, 1, 6, 9, 30)) == datetime(
        2025, 1, 6, 9, 45
    )


def test_crsd_converts_candle_open_to_completion_timestamp():
    instrument = InstrumentConfig(symbol="TEST", exchange="NSE")
    strategy = CRSDStrategy(
        instrument,
        StrategyConfig(candle_timeframe_minutes=15),
        ctx=None,
    )

    assert strategy._event_timestamp(datetime(2025, 1, 6, 9, 30)) == datetime(
        2025, 1, 6, 9, 45
    )


def test_ssf_without_persistent_runtime_is_unavailable_not_a_fresh_strategy(monkeypatch):
    service = PredictionService()
    instrument = InstrumentConfig(symbol="TEST", exchange="NSE")
    monkeypatch.setattr(service, "_validate_book", lambda *args, **kwargs: None)

    result = service._evaluate_ssf_l5_srm(
        instrument,
        data=None,
        ltp=100.0,
        book_snapshot=object(),
        ssf_strategy=None,
    )

    assert result.status == "UNAVAILABLE"
    assert "persistent runtime" in result.reason
    assert result.direction is None
