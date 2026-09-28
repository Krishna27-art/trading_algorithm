"""
Tests for Sector Impulse Transmission (SIT) strategy.

These are structural/plug-in tests (interface conformance, no-crash across a
full session, config knobs behave), not edge validation — the strategy's
thresholds are calibration placeholders per its own module docstring, so
asserting exact trade counts/directions here would be testing noise.
"""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from config.settings import InstrumentConfig, StrategyConfig
from strategy.base_strategy import BaseStrategy, StrategySignal
from strategy.sector_impulse_strategy import PeerContext, SectorImpulseStrategy, SITConfig


def _synth(days=15, seed=1, base=1000.0, bars_per_day=25):
    rng = np.random.RandomState(seed)
    rows = []
    d0 = date(2025, 1, 1)
    px = base
    for d in range(days):
        day = d0 + timedelta(days=d)
        t0 = datetime.combine(day, datetime.min.time()) + timedelta(hours=9, minutes=15)
        for i in range(bars_per_day):
            o = px
            px = px * (1 + rng.normal(0, 0.003))
            h, l = max(o, px) * 1.001, min(o, px) * 0.999
            rows.append(dict(datetime=t0 + timedelta(minutes=15 * i), open=o, high=h, low=l,
                              close=px, volume=int(rng.randint(1000, 5000))))
    return pd.DataFrame(rows)


@pytest.fixture
def ctx():
    return PeerContext(leader=_synth(seed=2, base=2500), market=_synth(seed=3, base=24000),
                        sector=_synth(seed=4, base=5000))


@pytest.fixture
def strategy(ctx):
    inst = InstrumentConfig(symbol="TESTSTOCK", exchange="NSE")
    return SectorImpulseStrategy(inst, StrategyConfig(), ctx=ctx)


def test_is_base_strategy(strategy):
    assert isinstance(strategy, BaseStrategy)


def test_runs_without_context_and_stays_disabled():
    inst = InstrumentConfig(symbol="X", exchange="NSE")
    strat = SectorImpulseStrategy(inst, StrategyConfig(), ctx=None)
    strat.reset_session(date(2025, 1, 1))
    assert strat.model is None
    sig = strat.on_candle(
        {"datetime": datetime(2025, 1, 1, 9, 30), "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 100},
        vwap=100.0,
    )
    assert sig is None  # never trades without peer data


def test_full_session_runs_without_error(strategy, ctx):
    own = _synth(seed=1, base=1000)
    own["date"] = own["datetime"].dt.date
    days = list(own.groupby("date"))
    signals = []
    for idx, (d, day_df) in enumerate(days):
        lookback = pd.concat([dd for _, dd in days[:idx]], ignore_index=True) if idx else pd.DataFrame(columns=own.columns)
        strategy.seed_context(lookback)
        strategy.reset_session(d)
        for _, row in day_df.iterrows():
            candle = dict(datetime=row.datetime, open=row.open, high=row.high, low=row.low,
                          close=row.close, volume=row.volume)
            sig = strategy.on_candle(candle, row.close)
            if sig is not None:
                assert isinstance(sig, StrategySignal)
                signals.append(sig)
                if sig.action.value in ("BUY", "SELL") and strategy.position == 0:
                    strategy.register_trade_entry(sig.price, 1 if sig.action.value == "BUY" else -1,
                                                  sig.stop_loss, sig.target, abs(sig.price - sig.stop_loss))
                elif sig.action.value == "EXIT":
                    strategy.register_trade_exit()
    # No assertion on signal count/direction (calibration-dependent) — the
    # point of this test is that a 15-day multi-symbol session never raises.


def test_on_tick_noop_when_flat(strategy):
    strategy.reset_session(date(2025, 1, 1))
    assert strategy.on_tick(1000.0, datetime(2025, 1, 1, 10, 0)) is None


def test_on_tick_exits_at_time_square_off(strategy):
    strategy.reset_session(date(2025, 1, 1))
    strategy.register_trade_entry(1000.0, 1, 990.0, 1020.0, 10.0)
    sig = strategy.on_tick(1005.0, datetime(2025, 1, 1, 15, 11))
    assert sig is not None
    assert sig.action.value == "EXIT"
    assert sig.reason == "TIME_SQUARE_OFF"


def test_disabled_on_expiry_day(strategy):
    from strategy.sector_impulse_strategy import _last_monthly_weekday
    expiry = _last_monthly_weekday(date(2025, 1, 15), SITConfig().expiry_weekday)
    strategy.reset_session(expiry)
    assert strategy.model is None
    assert strategy.disabled_reason == "expiry_day"
