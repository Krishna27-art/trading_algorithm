"""
Tests for SSF-L5-SRM strategy.

Structural/plug-in tests only — most of this strategy's numeric thresholds
are documented PLACEHOLDERs (spec's formula/weights were lost), so these
assert interface conformance and no-crash behavior over a book-update
stream, not specific entries.
"""

from datetime import date, datetime, timedelta

import numpy as np
import pytest

from config.settings import InstrumentConfig, StrategyConfig
from strategy.base_strategy import BaseStrategy, StrategySignal
from strategy.ssf_l5_srm_strategy import BookSnapshot, SsfL5SrmStrategy, select_candidates


@pytest.fixture
def strategy():
    inst = InstrumentConfig(symbol="TESTSTOCK", exchange="NSE")
    strat = SsfL5SrmStrategy(inst, StrategyConfig())
    strat.reset_session(date(2025, 1, 2))
    return strat


def _book(ts, mid, tick=0.05):
    bids = [(round(mid - tick * (k + 1), 2), 100, 1) for k in range(5)]
    asks = [(round(mid + tick * (k + 1), 2), 100, 1) for k in range(5)]
    return BookSnapshot(timestamp=ts, bids=bids, asks=asks, ltp=mid,
                        fut_ltp=mid * 1.001, fut_oi=100000.0,
                        sector_ret_30m=0.0, stock_ret_30m=0.0)


def test_is_base_strategy(strategy):
    assert isinstance(strategy, BaseStrategy)


def test_book_stream_runs_without_error(strategy):
    rng = np.random.RandomState(7)
    t0 = datetime(2025, 1, 2, 9, 45)
    mid = 1000.0
    for i in range(200):
        ts = t0 + timedelta(seconds=2 * i)
        mid *= 1 + rng.normal(0, 0.0004)
        if i < 30:
            candle = dict(datetime=ts, open=mid, high=mid * 1.002, low=mid * 0.998, close=mid, volume=500)
            strategy.on_candle(candle, mid)
        sig = strategy.on_book_update(_book(ts, mid))
        if sig is not None:
            assert isinstance(sig, StrategySignal)


def test_no_trade_in_opening_window(strategy):
    strategy.regime_ok = True
    ts = datetime(2025, 1, 2, 9, 20)
    f = {"spread_ticks": 1, "score": 10.0}
    assert strategy._blocked(_book(ts, 1000.0), f) == "no_trade_window"


def test_wide_spread_blocks_entry(strategy):
    strategy.regime_ok = True
    ts = datetime(2025, 1, 2, 10, 0)
    f = {"spread_ticks": 5, "score": 10.0}
    assert strategy._blocked(_book(ts, 1000.0), f) == "wide_spread"


def test_on_tick_time_stop(strategy):
    strategy.register_trade_entry(1000.0, 1, 995.0, 1010.0, 5.0)
    strategy.entry_time = datetime(2025, 1, 2, 10, 0)
    sig = strategy.on_tick(1002.0, datetime(2025, 1, 2, 10, 16))
    assert sig is not None and sig.reason == "TIME_STOP_15M"


def test_select_candidates_ranks_and_trims():
    basis_z = {"A": 0.5, "B": 2.0, "C": -3.0}
    resid_var = {"A": 1.0, "B": 2.0, "C": 3.0}
    picked = select_candidates(basis_z, resid_var, universe_top=3, pick=2)
    assert picked == ["C", "B"]  # ranked by |basis_z| within the top-variance set
