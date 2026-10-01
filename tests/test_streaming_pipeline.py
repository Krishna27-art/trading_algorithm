from datetime import datetime, time
import pandas as pd
import pytest

from streaming.live_market_state import LiveMarketState, live_market_state
from streaming.live_signal_engine import LiveSignalEngine, live_signal_engine
from streaming.market_stream_manager import (
    MarketStreamManager,
    StreamState,
    market_stream_manager,
)


@pytest.fixture(autouse=True)
def reset_streaming_state():
    live_market_state.reset()
    live_signal_engine.reset()
    market_stream_manager.stop_stream()
    yield
    live_market_state.reset()
    live_signal_engine.reset()
    market_stream_manager.stop_stream()


def test_stream_manager_initial_state_and_status():
    status = market_stream_manager.get_status()
    assert status["state"] == StreamState.STOPPED.value or status["state"] == StreamState.DISCONNECTED.value
    assert status["connected"] is False
    assert status["tick_count"] == 0
    assert status["candle_count"] == 0


def test_live_market_state_tick_processing():
    token_map = {738561: "RELIANCE"}
    live_market_state.set_token_map(token_map)

    dt1 = datetime(2026, 10, 1, 9, 15, 0)
    live_market_state.update_tick(
        symbol="RELIANCE",
        price=2500.0,
        volume=100,
        timestamp=dt1,
        token=738561,
    )

    state = live_market_state.get_symbol_state("RELIANCE")
    assert state is not None
    assert state.ltp == 2500.0
    assert state.open == 2500.0
    assert state.high == 2500.0
    assert state.low == 2500.0
    assert state.volume == 100

    dt2 = datetime(2026, 10, 1, 9, 16, 0)
    live_market_state.update_tick(
        symbol="RELIANCE",
        price=2510.0,
        volume=50,
        timestamp=dt2,
        token=738561,
    )

    assert state.ltp == 2510.0
    assert state.high == 2510.0
    assert state.low == 2500.0
    assert state.volume == 150


def test_live_market_state_and_signal_engine_candle_close():
    token_map = {738561: "RELIANCE"}
    live_market_state.set_token_map(token_map)

    # Seed with 25 candles to establish historical baseline
    for i in range(25):
        c = {
            "symbol": "RELIANCE",
            "datetime": datetime(2026, 9, 30, 9, 15) + pd.Timedelta(minutes=15 * i),
            "open": 2400.0 + i,
            "high": 2405.0 + i,
            "low": 2395.0 + i,
            "close": 2402.0 + i,
            "volume": 1000,
        }
        live_market_state.update_candle_close(c, vwap=2402.0 + i)

    # New completed candle today at 09:30 (09:15-09:30 completed bar)
    today_candle = {
        "symbol": "RELIANCE",
        "datetime": datetime(2026, 10, 1, 9, 15),
        "open": 2450.0,
        "high": 2465.0,
        "low": 2445.0,
        "close": 2460.0,
        "volume": 5000,
    }
    live_market_state.update_candle_close(today_candle, vwap=2458.0)

    # Trigger live signal engine
    res = live_signal_engine.on_candle_close(today_candle, vwap=2458.0)
    assert res is not None
    assert res["symbol"] == "RELIANCE"
    assert "predictions" in res
    assert "consensus" in res
    assert "orb" in res["predictions"]
    assert "cpr" in res["predictions"]
    assert "dual_ema" in res["predictions"]
    assert "apex" in res["predictions"]

    # Verify predictions are stored in engine
    stored = live_signal_engine.get_prediction("RELIANCE")
    assert stored is not None
    assert stored["symbol"] == "RELIANCE"


def test_live_signal_engine_with_level_5_depth():
    from strategy.ssf_l5_srm_strategy import BookSnapshot

    token_map = {738561: "RELIANCE"}
    live_market_state.set_token_map(token_map)

    snapshot = BookSnapshot(
        timestamp=datetime(2026, 10, 1, 10, 0, 0),
        bids=[(2500.0 - 0.05 * i, 1000 * (5 - i), 10) for i in range(5)],
        asks=[(2500.05 + 0.05 * i, 200 * (i + 1), 2) for i in range(5)],
        ltp=2500.0,
    )
    live_market_state.update_book_snapshot("RELIANCE", snapshot)

    state = live_market_state.get_symbol_state("RELIANCE")
    assert state.book_snapshot is not None
    assert state.book_snapshot.ltp == 2500.0


def test_fastapi_stream_endpoints():
    from backend.stream import stream_market, stream_signals, stream_status

    # Status returns health dictionary
    st = stream_status()
    assert "state" in st
    assert "connected" in st
    assert "subscribed_token_count" in st

    # Seed state for signals and market
    live_market_state.update_tick("TCS", 3500.0, 50, datetime(2026, 10, 1, 10, 0), token=2953217)
    live_signal_engine._predictions["TCS"] = {
        "symbol": "TCS",
        "ltp": 3500.0,
        "consensus": {"direction": "LONG", "label": "STRONG LONG (4/4)"},
    }

    m_res = stream_market()
    assert m_res["status"] == "success"
    assert "TCS" in m_res["instruments"]
    assert m_res["instruments"]["TCS"]["ltp"] == 3500.0

    s_res = stream_signals()
    assert s_res["status"] == "success"
    assert "TCS" in s_res["signals"]
    assert s_res["signals"]["TCS"]["consensus"]["direction"] == "LONG"
