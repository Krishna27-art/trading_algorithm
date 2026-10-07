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

from backend.config.settings import InstrumentConfig, StrategyConfig
from backend.strategy.base_strategy import BaseStrategy, StrategySignal
from backend.strategy.ssf_l5_srm_strategy import BookSnapshot, SsfL5SrmStrategy, select_candidates


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


def test_make_book_snapshot_from_quote():
    """Verifies KiteBrokerAdapter parses real Kite quote depth into BookSnapshot."""
    from backend.broker.kite_adapter import KiteBrokerAdapter

    quote_dict = {
        "instrument_token": 779521,
        "last_price": 780.50,
        "oi": 15000000,
        "lower_circuit_limit": 702.0,
        "upper_circuit_limit": 858.0,
        "timestamp": "2025-01-02T10:30:00",
        "depth": {
            "buy": [
                {"price": 780.45, "quantity": 1000, "orders": 5},
                {"price": 780.40, "quantity": 2500, "orders": 12},
                {"price": 780.35, "quantity": 5000, "orders": 20},
                {"price": 780.30, "quantity": 3000, "orders": 15},
                {"price": 780.25, "quantity": 8000, "orders": 35},
            ],
            "sell": [
                {"price": 780.55, "quantity": 1200, "orders": 6},
                {"price": 780.60, "quantity": 2000, "orders": 10},
                {"price": 780.65, "quantity": 4500, "orders": 18},
                {"price": 780.70, "quantity": 6000, "orders": 22},
                {"price": 780.75, "quantity": 7500, "orders": 30},
            ],
        },
    }

    snap = KiteBrokerAdapter.make_book_snapshot_from_quote(quote_dict, "SBIN")
    assert snap is not None
    assert isinstance(snap, BookSnapshot)
    assert snap.ltp == 780.50
    assert len(snap.bids) == 5
    assert len(snap.asks) == 5
    assert snap.bids[0] == (780.45, 1000, 5)
    assert snap.asks[0] == (780.55, 1200, 6)
    assert snap.circuit_lower == 702.0
    assert snap.circuit_upper == 858.0


def test_multi_symbol_candle_aggregator_buffers_and_dispatches_depth():
    """Verifies MultiSymbolCandleAggregator extracts Level-5 depth from ticks and fires callback."""
    from backend.data.candle_aggregator import MultiSymbolCandleAggregator

    received_snapshots = []

    def on_book(sym: str, snap: BookSnapshot):
        received_snapshots.append((sym, snap))

    agg = MultiSymbolCandleAggregator(
        token_to_symbol_map={779521: "SBIN"},
        timeframe_minutes=15,
        on_book_update=on_book,
    )

    tick = {
        "instrument_token": 779521,
        "last_price": 780.50,
        "last_traded_quantity": 50,
        "volume_traded": 100000,
        "timestamp": datetime(2025, 1, 2, 10, 0),
        "depth": {
            "buy": [{"price": 780.0 - i * 0.05, "quantity": 100, "orders": 1} for i in range(5)],
            "sell": [{"price": 780.1 + i * 0.05, "quantity": 100, "orders": 1} for i in range(5)],
        },
    }

    agg.process_ticks([tick])

    latest = agg.get_latest_book_snapshot("SBIN")
    assert latest is not None
    assert len(received_snapshots) == 1
    assert received_snapshots[0][0] == "SBIN"
    assert received_snapshots[0][1].ltp == 780.50


def test_ssf_signal_generation_on_favorable_microstructure(strategy):
    """Verifies SSF strategy generates BUY signal when regime is OK and order flow/basis triggers."""
    strategy.regime_ok = True
    t0 = datetime(2025, 1, 2, 10, 0)
    strategy.reset_session(t0.date())
    strategy.regime_ok = True

    # Feed baseline snapshots to warm up Z-score buffers with realistic mid variation
    for i in range(70):
        ts = t0 + timedelta(seconds=i)
        base = 100.0 + (i % 5) * 0.10
        snap = BookSnapshot(
            timestamp=ts,
            bids=[(base - k * 0.05, 100, 1) for k in range(5)],
            asks=[(base + 0.10 + k * 0.05, 100, 1) for k in range(5)],
            ltp=base,
            fut_ltp=base + 0.05,
            fut_oi=100000.0 + (i % 10) * 50.0,
            sector_ret_30m=0.001,
            stock_ret_30m=0.001 + (i % 5) * 0.0002,
        )
        strategy.on_book_update(snap)

    # Now create massive buying imbalance (MLOFI + positive basis + positive sector residual)
    impulse_snap = BookSnapshot(
        timestamp=t0 + timedelta(seconds=71),
        bids=[(100.50 - k * 0.05, 5000, 10) for k in range(5)],  # Bid price & quantity surged
        asks=[(100.55 + k * 0.05, 20, 1) for k in range(5)],
        ltp=100.50,
        fut_ltp=101.50,  # Futures surged
        fut_oi=150000.0, # OI surged
        sector_ret_30m=0.001,
        stock_ret_30m=0.010, # Outperforming sector
    )

    sig = strategy.on_book_update(impulse_snap)
    assert sig is not None
    assert sig.action.value == "BUY"
    assert sig.price == 100.50
    assert sig.stop_loss < sig.price
    assert sig.target > sig.price
