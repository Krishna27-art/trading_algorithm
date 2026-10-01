from datetime import date, datetime, time

import pandas as pd

from config.settings import InstrumentConfig, StrategyConfig
from strategy.apex_engine import ApexStrategy, EngineConfig


def _history():
    rows = []
    px = 1000.0

    for i in range(30):
        ts = datetime(2025, 1, 1, 9, 15) + pd.Timedelta(minutes=15 * i)
        rows.append(
            {
                "datetime": ts,
                "open": px,
                "high": px + 5,
                "low": px - 5,
                "close": px + 1,
                "volume": 1000,
            }
        )
        px += 1

    return pd.DataFrame(rows)


def test_apex_gap_uses_0915_opening_bar():
    inst = InstrumentConfig(symbol="TEST", exchange="NSE")

    strategy = ApexStrategy(
        inst,
        StrategyConfig(),
        config=EngineConfig(
            entry_start=time(9, 30),
            entry_end=time(13, 30),
            market_open=time(9, 15),
        ),
    )

    prior = _history()

    strategy.seed_context(prior)

    session = date(2025, 1, 2)
    strategy.reset_session(session)

    # Actual market-open candle.
    opening_candle = {
        "datetime": datetime(2025, 1, 2, 9, 15),
        "open": 1100.0,
        "high": 1110.0,
        "low": 1095.0,
        "close": 1105.0,
        "volume": 5000,
    }

    strategy.on_candle(opening_candle, vwap=1102.0)

    assert strategy.today_open == 1100.0
    assert strategy._opening_candle_seen is True

    # A later candle must NOT overwrite today's opening price.
    later_candle = {
        "datetime": datetime(2025, 1, 2, 9, 30),
        "open": 1200.0,
        "high": 1210.0,
        "low": 1195.0,
        "close": 1205.0,
        "volume": 6000,
    }

    strategy.on_candle(later_candle, vwap=1202.0)

    assert strategy.today_open == 1100.0


def test_apex_missing_0915_open_does_not_use_0930_open():
    inst = InstrumentConfig(symbol="TEST", exchange="NSE")

    strategy = ApexStrategy(
        inst,
        StrategyConfig(),
        config=EngineConfig(
            entry_start=time(9, 30),
            entry_end=time(13, 30),
            market_open=time(9, 15),
        ),
    )

    strategy.seed_context(_history())
    strategy.reset_session(date(2025, 1, 2))

    late_candle = {
        "datetime": datetime(2025, 1, 2, 9, 30),
        "open": 1200.0,
        "high": 1210.0,
        "low": 1195.0,
        "close": 1205.0,
        "volume": 6000,
    }

    strategy.on_candle(late_candle, vwap=1202.0)

    assert strategy.today_open is None
    assert strategy._opening_candle_seen is False

    features = strategy._calculate_features(
        {
            "datetime": datetime(2025, 1, 2, 9, 30),
            "open": 1200.0,
            "high": 1210.0,
            "low": 1195.0,
            "close": 1205.0,
            "volume": 6000,
        },
        vwap=1202.0,
    )

    assert features["gap_atr"] is None
