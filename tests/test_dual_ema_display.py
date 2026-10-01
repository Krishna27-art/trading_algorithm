from datetime import datetime, date

import pandas as pd

from config.settings import InstrumentConfig, InstrumentType, StrategyConfig
from strategy.dual_ema_strategy import BufferedDualEMAStrategy
from strategy.prediction_service import PredictionService


def _instrument():
    return InstrumentConfig(
        symbol="RELIANCE",
        exchange="NSE",
        instrument_type=InstrumentType.EQUITY,
        lot_size=1,
        tick_size=0.05,
        min_orb_range=5.0,
        max_orb_range=50.0,
        max_risk_cap=30.0,
    )


def _candle(dt, price):
    return {
        "datetime": dt,
        "open": price,
        "high": price + 1.0,
        "low": price - 1.0,
        "close": price,
        "volume": 10000,
    }


def test_latest_indicators_does_not_append_final_candle():
    strategy = BufferedDualEMAStrategy(
        instrument=_instrument(),
        strategy_config=StrategyConfig(),
    )

    strategy.reset_session(datetime(2026, 10, 1).date())

    # Provide enough historical bars for SMA200.
    history_rows = []
    start = datetime(2026, 9, 1, 9, 15)

    for i in range(200):
        dt = start + pd.Timedelta(minutes=15 * i)
        price = 100.0 + (i * 0.05)

        history_rows.append(
            {
                "datetime": dt,
                "open": price,
                "high": price + 1.0,
                "low": price - 1.0,
                "close": price,
            }
        )

    strategy.seed_context(
        pd.DataFrame(history_rows)
    )

    candle = _candle(
        datetime(2026, 10, 1, 9, 15),
        120.0,
    )

    strategy.on_candle(candle, 120.0)

    count_before = len(strategy.today_bars)

    assert count_before == 1

    strategy.latest_indicators()

    count_after = len(strategy.today_bars)

    assert count_after == count_before


def test_on_candle_stores_one_observation_for_one_source_candle():
    strategy = BufferedDualEMAStrategy(
        instrument=_instrument(),
        strategy_config=StrategyConfig(),
    )

    strategy.reset_session(datetime(2026, 10, 1).date())

    candle = _candle(
        datetime(2026, 10, 1, 9, 30),
        120.0,
    )

    strategy.on_candle(candle, 120.0)

    assert len(strategy.today_bars) == 1

    stored = strategy.today_bars[0]

    assert stored["datetime"] == datetime(
        2026, 10, 1, 9, 30
    )


def test_latest_indicators_uses_existing_strategy_state_without_raw_timestamp_append():
    strategy = BufferedDualEMAStrategy(
        instrument=_instrument(),
        strategy_config=StrategyConfig(),
    )

    strategy.reset_session(datetime(2026, 10, 1).date())

    candle = _candle(
        datetime(2026, 10, 1, 9, 30),
        120.0,
    )

    strategy.on_candle(candle, 120.0)

    before = list(strategy.today_bars)

    strategy.latest_indicators()

    after = list(strategy.today_bars)

    assert after == before
    assert len(after) == 1


def test_prediction_service_evaluate_dual_ema_does_not_duplicate_final_candle(monkeypatch):
    inst = _instrument()
    service = PredictionService()

    # Create 2 days of 15m candles (25 candles per day)
    rows = []
    start_day1 = datetime(2026, 9, 30, 9, 15)
    for i in range(25):
        dt = start_day1 + pd.Timedelta(minutes=15 * i)
        rows.append({
            "datetime": dt,
            "open": 100.0 + i,
            "high": 101.0 + i,
            "low": 99.0 + i,
            "close": 100.5 + i,
            "volume": 1000,
        })

    start_day2 = datetime(2026, 10, 1, 9, 15)
    today_candle_count = 5
    for i in range(today_candle_count):
        dt = start_day2 + pd.Timedelta(minutes=15 * i)
        rows.append({
            "datetime": dt,
            "open": 130.0 + i,
            "high": 131.0 + i,
            "low": 129.0 + i,
            "close": 130.5 + i,
            "volume": 1000,
        })

    df_15m = pd.DataFrame(rows)

    captured_instances = []
    original_init = BufferedDualEMAStrategy.__init__

    def spy_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        captured_instances.append(self)

    monkeypatch.setattr(BufferedDualEMAStrategy, "__init__", spy_init)

    prediction = service._evaluate_dual_ema(
        inst=inst,
        df_15m=df_15m,
        ltp=135.0,
    )

    assert len(captured_instances) == 1
    strat = captured_instances[0]

    # Exactly today_candle_count bars ingested, without the extra display append
    assert len(strat.today_bars) == today_candle_count
    assert prediction is not None
