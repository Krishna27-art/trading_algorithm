from dataclasses import replace
from datetime import datetime

import numpy as np
import pandas as pd

from backend.config.settings import InstrumentConfig, StrategyConfig
from backend.data.models import BookSnapshot
from backend.strategy.crsd_strategy import CRSDStrategy
from backend.strategy.prediction_service import PredictionService
from backend.strategy.sector_impulse_strategy import (
    SectorImpulseStrategy,
    _session_rolling_sum,
)
from backend.strategy.ssf_l5_srm_strategy import SsfL5SrmStrategy


def _instrument():
    return InstrumentConfig(symbol="TEST", exchange="NSE")


def test_sector_impulse_rolling_window_does_not_bridge_sessions():
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
    assert np.isnan(result.iloc[2])
    assert result.iloc[3] == 104.0


def test_sector_impulse_candle_open_maps_to_completion_time():
    strategy = SectorImpulseStrategy(
        _instrument(),
        StrategyConfig(candle_timeframe_minutes=15),
        ctx=None,
    )

    assert strategy._event_timestamp(datetime(2025, 1, 6, 9, 30)) == datetime(
        2025, 1, 6, 9, 45
    )


def test_crsd_candle_open_maps_to_completion_time():
    strategy = CRSDStrategy(
        _instrument(),
        StrategyConfig(candle_timeframe_minutes=15),
        ctx=None,
    )

    assert strategy._event_timestamp(datetime(2025, 1, 6, 9, 30)) == datetime(
        2025, 1, 6, 9, 45
    )


def test_ssf_missing_persistent_runtime_returns_unavailable(monkeypatch):
    service = object.__new__(PredictionService)
    monkeypatch.setattr(service, "_validate_book", lambda *args, **kwargs: None)

    result = service._evaluate_ssf_l5_srm(
        _instrument(),
        data=None,
        ltp=100.0,
        book_snapshot=object(),
        ssf_strategy=None,
    )

    assert result.status == "UNAVAILABLE"
    assert "persistent runtime" in result.reason
    assert result.direction is None


def test_ssf_deduplicates_only_identical_book_snapshots(monkeypatch):
    strategy = SsfL5SrmStrategy(
        _instrument(),
        StrategyConfig(),
        signal_only=True,
    )
    ts = datetime(2025, 1, 6, 10, 0)
    bids = [(100.0 - i * 0.05, 100 + i, 1) for i in range(5)]
    asks = [(100.05 + i * 0.05, 120 + i, 1) for i in range(5)]
    first = BookSnapshot(timestamp=ts, bids=bids, asks=asks, ltp=100.0)
    changed = replace(
        first,
        bids=[(100.0 - i * 0.05, 200 + i, 1) for i in range(5)],
    )
    observed = []

    monkeypatch.setattr(
        strategy,
        "_features",
        lambda snapshot: observed.append(snapshot) or {},
    )
    strategy._prev = first

    assert strategy.on_book_update(first) is None
    assert observed == []

    assert strategy.on_book_update(changed) is None
    assert observed == [changed]
    assert strategy._prev == changed
