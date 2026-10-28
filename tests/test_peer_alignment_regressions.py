"""
Regression tests for peer/market context alignment defects.

1. Peer context ``as_of`` must be the evaluated bar's COMPLETION time. Passing
   the bar's open time resolves to the previous completed bar, so Sector
   Impulse paired the stock's newest bar with the leader/market bar before it
   and CRSD (exact-timestamp lookups) found no peer bar at all.
2. Only NIFTY may update the CRSD market frame; sector-index / INDIA VIX
   candles must not overwrite it.
3. Unclassified symbols must not be assigned an arbitrary default sector.
4. Peer frames are topped up from real completed candles already held in
   LiveMarketState (no synthetic rows) before any network fetch.

All candles here are test fixtures used only to exercise timestamp plumbing;
they make no claim about live-market behaviour.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from backend.config.universe import create_instrument_config_for_equity
from backend.data import sector_peer_manager as spm
from backend.data.sector_peer_manager import (
    SectorPeerManager,
    live_completed_candles,
    merge_with_live_candles,
)
from backend.streaming.live_market_state import live_market_state
from backend.streaming.market_stream_manager import _index_candle_route


SESSION = date(2026, 10, 27)  # a past Tuesday


def _bars(symbol: str, day: date, n: int, base: float = 100.0) -> pd.DataFrame:
    rows = []
    t = datetime.combine(day, time(9, 15))
    for i in range(n):
        o = base + i * 0.1
        rows.append(
            {
                "datetime": t,
                "open": o,
                "high": o + 0.5,
                "low": o - 0.5,
                "close": o + 0.2,
                "volume": 1000 + i,
            }
        )
        t += timedelta(minutes=15)
    return pd.DataFrame(rows)


@pytest.fixture(autouse=True)
def _clean_live_state():
    live_market_state.reset()
    yield
    live_market_state.reset()


# ---------------------------------------------------------------------------
# 1. as_of alignment
# ---------------------------------------------------------------------------

def _write_peer_cache(tmp_path, symbols, n_bars):
    for s in symbols:
        _bars(s, SESSION, n_bars).to_csv(tmp_path / f"{s}_15m.csv", index=False)


def test_peer_context_open_time_resolves_to_previous_bar(tmp_path):
    """Documents the defect: open-time as_of drops the evaluated bar."""
    leader, market, sector = SectorPeerManager.get_peer_symbols("WIPRO")
    _write_peer_cache(tmp_path, {leader, market, sector}, n_bars=11)  # through 11:45

    bar_open = datetime.combine(SESSION, time(11, 45))
    res = SectorPeerManager.build_peer_context_with_status(
        "WIPRO", cache_dir=tmp_path, as_of=bar_open
    )
    assert res.required_timestamp == datetime.combine(SESSION, time(11, 30))


def test_peer_context_close_time_ends_on_evaluated_bar(tmp_path):
    leader, market, sector = SectorPeerManager.get_peer_symbols("WIPRO")
    _write_peer_cache(tmp_path, {leader, market, sector}, n_bars=11)

    bar_open = datetime.combine(SESSION, time(11, 45))
    res = SectorPeerManager.build_peer_context_with_status(
        "WIPRO", cache_dir=tmp_path, as_of=bar_open + timedelta(minutes=15)
    )
    assert res.status == spm.PEER_STATUS_AVAILABLE, res.reason
    assert res.required_timestamp == bar_open
    for frame in res.context.frames.values():
        assert frame.index[-1] == pd.Timestamp(bar_open)


def test_prepared_candles_expose_completion_time():
    from backend.strategy.prediction_service import PredictionService

    svc = PredictionService()
    df = pd.concat(
        [_bars("WIPRO", SESSION - timedelta(days=1), 25), _bars("WIPRO", SESSION, 11)],
        ignore_index=True,
    )
    df["vwap"] = df["close"]
    prep = svc._build_prepared("WIPRO", df, datetime.combine(SESSION, time(12, 0, 5)))
    assert prep.ok, prep.reason
    assert prep.latest_candle_time == datetime.combine(SESSION, time(11, 45))
    assert prep.latest_candle_close_time == datetime.combine(SESSION, time(12, 0))


def test_sector_impulse_builder_receives_completion_time(monkeypatch):
    from backend.strategy.prediction_service import PredictionService

    svc = PredictionService()
    df = pd.concat(
        [_bars("WIPRO", SESSION - timedelta(days=1), 25), _bars("WIPRO", SESSION, 11)],
        ignore_index=True,
    )
    df["vwap"] = df["close"]
    prep = svc._build_prepared("WIPRO", df, datetime.combine(SESSION, time(12, 0, 5)))
    assert prep.ok, prep.reason
    prep.warmup_complete = True

    seen = {}

    def fake_build(**kwargs):
        seen["as_of"] = kwargs.get("as_of")
        return spm.PeerContextResult(
            status=spm.PEER_STATUS_UNAVAILABLE, context=None, reason="test: no peers"
        )

    monkeypatch.setattr(
        SectorPeerManager, "build_peer_context_with_status", staticmethod(fake_build)
    )
    inst = create_instrument_config_for_equity("WIPRO", 3787777, current_price=100.0, lot_size=1)
    pred = svc._evaluate_sector_impulse(inst, prep, 101.0)

    assert seen["as_of"] == datetime.combine(SESSION, time(12, 0))
    assert pred.status == "UNAVAILABLE"


def test_live_engine_passes_candle_completion_time(monkeypatch):
    from backend.data.time_utils import now_ist_naive
    import importlib

    lse_mod = importlib.import_module("backend.streaming.live_signal_engine")
    from backend.streaming.live_signal_engine import LiveSignalEngine

    engine = LiveSignalEngine()
    seen = {}

    def fake_get_context(symbol, kite_client=None, as_of=None):
        seen["crsd"] = as_of
        return None

    def fake_peer(symbol, **kwargs):
        seen["peer"] = kwargs.get("as_of")
        return None

    monkeypatch.setattr(engine._crsd_runtime, "get_context", fake_get_context)
    monkeypatch.setattr(SectorPeerManager, "build_peer_context", staticmethod(fake_peer))
    monkeypatch.setattr(
        lse_mod.prediction_service,
        "evaluate_symbol",
        lambda **kw: ({}, lse_mod.prediction_service.calculate_consensus({})),
    )

    live_market_state.set_token_map({3787777: "WIPRO"})
    engine.record_tick_price("WIPRO", 101.0, now_ist_naive())

    candle_open = datetime.combine(SESSION, time(11, 45))
    candle = {
        "symbol": "WIPRO",
        "datetime": candle_open,
        "open": 100.0,
        "high": 101.5,
        "low": 99.5,
        "close": 101.0,
        "volume": 1000,
    }
    live_market_state.update_candle_close(candle, vwap=100.5)
    engine.on_candle_close(candle, vwap=100.5)

    assert seen["crsd"] == candle_open + timedelta(minutes=15)
    assert seen["peer"] == candle_open + timedelta(minutes=15)


# ---------------------------------------------------------------------------
# 2. CRSD market frame routing
# ---------------------------------------------------------------------------

def test_only_nifty_updates_crsd_market_frame():
    index_symbols = {"NIFTY", "NIFTY IT", "NIFTY BANK", "INDIA VIX"}
    assert _index_candle_route("NIFTY", index_symbols) == "market"
    assert _index_candle_route("INDIA VIX", index_symbols) == "index_context"
    assert _index_candle_route("NIFTY IT", index_symbols) == "index_context"
    assert _index_candle_route("NIFTY BANK", index_symbols) == "index_context"
    assert _index_candle_route("INFY", index_symbols) == "stock"


# ---------------------------------------------------------------------------
# 3. No fabricated sector
# ---------------------------------------------------------------------------

def test_unclassified_symbol_has_no_default_sector():
    assert SectorPeerManager.get_sector_for_symbol("NOT_A_REAL_SYMBOL_XYZ") is None
    with pytest.raises(ValueError):
        SectorPeerManager.get_peer_symbols("NOT_A_REAL_SYMBOL_XYZ")


# ---------------------------------------------------------------------------
# 4. Live-state top-up (no synthetic rows)
# ---------------------------------------------------------------------------

def test_merge_with_live_candles_tops_up_stale_cache():
    cached = _bars("TCS", SESSION, 4)          # 09:15 .. 10:00
    live = _bars("TCS", SESSION, 11)           # 09:15 .. 11:45
    live_market_state.seed_historical_candles("TCS", live)

    merged = merge_with_live_candles(cached, "TCS")
    assert merged["datetime"].iloc[-1] == pd.Timestamp(datetime.combine(SESSION, time(11, 45)))
    assert len(merged) == 11                    # union only, nothing synthesised
    assert merged["datetime"].is_unique


def test_merge_without_live_candles_returns_cache_unchanged():
    cached = _bars("TCS", SESSION, 4)
    assert live_completed_candles("TCS") is None
    assert merge_with_live_candles(cached, "TCS") is cached


def test_peer_context_uses_live_candles_when_cache_is_stale(tmp_path):
    leader, market, sector = SectorPeerManager.get_peer_symbols("WIPRO")
    _write_peer_cache(tmp_path, {leader, market, sector}, n_bars=4)  # stale: ends 10:00
    for s in {leader, market, sector}:
        live_market_state.seed_historical_candles(s, _bars(s, SESSION, 11))

    bar_close = datetime.combine(SESSION, time(12, 0))
    res = SectorPeerManager.build_peer_context_with_status(
        "WIPRO", cache_dir=tmp_path, as_of=bar_close, allow_network_fetch=False
    )
    assert res.status == spm.PEER_STATUS_AVAILABLE, res.reason
    for frame in res.context.frames.values():
        assert frame.index[-1] == pd.Timestamp(datetime.combine(SESSION, time(11, 45)))


def test_peer_context_still_unavailable_when_live_is_also_stale(tmp_path):
    leader, market, sector = SectorPeerManager.get_peer_symbols("WIPRO")
    _write_peer_cache(tmp_path, {leader, market, sector}, n_bars=4)
    for s in {leader, market, sector}:
        live_market_state.seed_historical_candles(s, _bars(s, SESSION, 6))  # ends 10:30

    res = SectorPeerManager.build_peer_context_with_status(
        "WIPRO",
        cache_dir=tmp_path,
        as_of=datetime.combine(SESSION, time(12, 0)),
        allow_network_fetch=False,
    )
    assert res.status == spm.PEER_STATUS_UNAVAILABLE
    assert res.stale is True
