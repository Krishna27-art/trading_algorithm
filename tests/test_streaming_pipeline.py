from datetime import datetime, time
import pandas as pd
import pytest

from backend.streaming.live_market_state import LiveMarketState, live_market_state
from backend.streaming.live_signal_engine import LiveSignalEngine, live_signal_engine
from backend.streaming.market_stream_manager import (
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
        day_open=2500.0,
        day_high=2500.0,
        day_low=2500.0,
        session_volume=100,
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
        day_open=2500.0,
        day_high=2510.0,
        day_low=2500.0,
        session_volume=150,
    )

    state = live_market_state.get_symbol_state("RELIANCE")
    assert state is not None
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
    live_signal_engine.record_tick_price("RELIANCE", 2460.0, datetime.now())

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
    from backend.strategy.ssf_l5_srm_strategy import BookSnapshot

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


def test_fastapi_stream_endpoints(monkeypatch):
    from backend.routes.stream import stream_market, stream_signals, stream_status
    import backend.routes.stream as sr_mod
    from backend.data.time_utils import now_ist_naive

    # Status returns health dictionary
    st = stream_status()
    assert "state" in st
    assert "connected" in st
    assert "subscribed_token_count" in st

    monkeypatch.setattr(sr_mod, "_stream_status", lambda: {"state": "STREAMING", "connected": True, "subscribed_token_count": 1})

    now_ts = now_ist_naive()
    # Seed state for signals and market
    live_market_state.update_tick(
        "TCS",
        3500.0,
        50,
        now_ts,
        token=2953217,
        day_open=3500.0,
        day_high=3500.0,
        day_low=3500.0,
        session_volume=50,
    )
    live_signal_engine._predictions["TCS"] = {
        "symbol": "TCS",
        "ltp": 3500.0,
        "ltp_timestamp": now_ts.isoformat(),
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


def test_market_stream_manager_kite_client_propagation(monkeypatch):
    import sys
    from unittest.mock import MagicMock

    fake_kite = MagicMock()
    fake_kite.instruments.return_value = [
        {"instrument_token": 738561, "tradingsymbol": "RELIANCE", "exchange": "NSE", "lot_size": 1},
        {"instrument_token": 260000, "tradingsymbol": "NIFTY ENERGY", "exchange": "NSE", "lot_size": 1},
    ]
    mod = sys.modules["backend.streaming.market_stream_manager"]
    monkeypatch.setattr(mod, "get_active_kite", lambda: fake_kite)
    monkeypatch.setattr(mod, "get_saved_session", lambda: {"api_key": "k", "access_token": "tok"})

    # Mock KiteTicker so it doesn't open a real WebSocket
    mock_ticker_instance = MagicMock()
    monkeypatch.setattr("kiteconnect.KiteTicker", lambda api_key, access_token: mock_ticker_instance)

    token_map = {738561: "RELIANCE"}

    passed_client = []
    original_on_candle = live_signal_engine.on_candle_close

    def mock_on_candle(candle, vwap, kite_client=None):
        passed_client.append(kite_client)
        return original_on_candle(candle, vwap, kite_client=kite_client)

    monkeypatch.setattr(live_signal_engine, "on_candle_close", mock_on_candle)

    res = market_stream_manager.start_stream(token_to_symbol=token_map)
    assert res["status"] == "CONNECTING"

    # Set history ready so strategy evaluation is not skipped
    with market_stream_manager._history_lock:
        market_stream_manager._history_ready_symbols.add("RELIANCE")
    market_stream_manager._first_observed_candle.pop("RELIANCE", None)

    # Trigger aggregator's candle close callback
    candle = {
        "symbol": "RELIANCE",
        "datetime": datetime(2026, 10, 1, 9, 15),
        "open": 2500.0,
        "high": 2510.0,
        "low": 2490.0,
        "close": 2505.0,
        "volume": 1000,
    }
    live_signal_engine.record_tick_price("RELIANCE", 2505.0, datetime.now())
    market_stream_manager.aggregator.on_candle_close(candle, vwap=2502.0)

    import time
    time.sleep(0.2)

    assert len(passed_client) >= 1
    assert all(c is fake_kite for c in passed_client)


def test_market_stream_manager_raises_when_no_kite_client(monkeypatch):
    import sys

    mod = sys.modules["backend.streaming.market_stream_manager"]
    monkeypatch.setattr(mod, "get_active_kite", lambda: None)

    with pytest.raises(RuntimeError, match="No active Zerodha Kite client available"):
        market_stream_manager.start_stream(token_to_symbol={738561: "RELIANCE"})

    assert market_stream_manager.state == StreamState.ERROR


def test_seed_historical_candles_populates_live_history():
    state = LiveMarketState(max_candle_history=640)

    df = pd.DataFrame([
        {
            "datetime": "2026-09-30 09:15:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000,
        },
        {
            "datetime": "2026-10-01 15:15:00",
            "open": 102.0,
            "high": 103.0,
            "low": 101.0,
            "close": 102.5,
            "volume": 1200,
        },
    ])

    count = state.seed_historical_candles("TCS", df)
    assert count == 2

    seeded = state.get_candles_df("TCS")
    assert len(seeded) == 2

    # Verify LTP is NOT overwritten by historical candles
    state.update_tick(symbol="TCS", price=3500.0, volume=10, timestamp=datetime(2026, 10, 1, 15, 16, 0))
    state.seed_historical_candles("TCS", df)
    assert state.get_symbol_state("TCS").ltp == 3500.0


def test_market_stream_manager_timestamp_compatibility():
    """Verify get_status safely computes tick age for naive, IST, UTC and None timestamps."""
    from zoneinfo import ZoneInfo
    from backend.data.time_utils import now_ist

    manager = MarketStreamManager()

    # 1. Missing timestamps -> None age
    manager.last_tick_time = None
    manager.last_equity_tick_time = None
    st = manager.get_status()
    assert st["last_tick_age_seconds"] is None
    assert st["last_equity_tick_age_seconds"] is None

    # 2. Naive timestamps (e.g. from exchange/pandas)
    naive_now = datetime.now()
    manager.last_tick_time = naive_now
    manager.last_equity_tick_time = naive_now
    st = manager.get_status()
    assert st["last_tick_age_seconds"] is not None
    assert st["last_tick_age_seconds"] >= 0.0
    assert st["last_equity_tick_age_seconds"] is not None
    assert st["last_equity_tick_age_seconds"] >= 0.0

    # 3. Timezone-aware IST timestamps
    ist_now = now_ist()
    manager.last_tick_time = ist_now
    manager.last_equity_tick_time = ist_now
    st = manager.get_status()
    assert st["last_tick_age_seconds"] is not None
    assert st["last_tick_age_seconds"] >= 0.0
    assert st["last_equity_tick_age_seconds"] is not None
    assert st["last_equity_tick_age_seconds"] >= 0.0

    # 4. Timezone-aware UTC timestamps
    utc_now = datetime.now(ZoneInfo("UTC"))
    manager.last_tick_time = utc_now
    manager.last_equity_tick_time = utc_now
    st = manager.get_status()
    assert st["last_tick_age_seconds"] is not None
    assert st["last_tick_age_seconds"] >= 0.0
    assert st["last_equity_tick_age_seconds"] is not None
    assert st["last_equity_tick_age_seconds"] >= 0.0



