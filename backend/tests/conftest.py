"""
Test harness. The ZIP contains only backend/; the modules it imports
(config, broker, data, streaming, strategy, scanner, kiteconnect) are replaced
by controllable stubs so the REAL backend/*.py code is what gets exercised.
"""
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class State:
    """Mutable knobs the tests turn."""
    kite = None
    auth_err = None
    now = datetime(2026, 10, 2, 11, 0, 0)
    trading_day = True
    stream_status = {"state": "LIVE"}
    stream_raises = False
    predictions = {}
    predictions_calls = 0
    warmer_calls = 0
    eval_calls = []
    eval_result = None
    eval_raises = False
    universe = []
    token_map = {}
    market_state = {}
    fetch_calls = []


S = State


def _install_stubs():
    settings = SimpleNamespace(
        app_shared_secret="s3cret",
        kite_api_key="k", kite_api_secret="x",
        active_strategy="cpr",
        base_dir=ROOT, db_path=ROOT / "nope.db",
        instruments=[SimpleNamespace(
            symbol="NIFTY", exchange="NSE", lot_size=75, instrument_token=None,
            max_risk_cap=None, instrument_type=SimpleNamespace(value="INDEX"))],
        strategy=SimpleNamespace(risk_reward_ratio=2.0, breakeven_r_multiple=1.0),
        risk=SimpleNamespace(initial_capital=100000.0),
    )
    _mod("config")
    _mod("config.settings", settings=settings)

    class Rec(SimpleNamespace):
        pass

    class StockUniverse:
        @property
        def all_stocks(self):
            return S.universe

    _mod("config.universe",
         StockUniverse=StockUniverse,
         resolve_universe_tokens=lambda kite_client=None: dict(S.token_map),
         create_instrument_config_for_equity=lambda sym, tok: SimpleNamespace(
             symbol=sym, instrument_token=tok, max_risk_cap=None, lot_size=1))

    _mod("broker")
    _mod("broker.kite_adapter",
         get_active_kite=lambda: S.kite,
         get_active_kite_with_diagnostics=lambda force_validate=False: (S.kite, S.auth_err),
         get_saved_session=lambda: None,
         save_session=lambda **k: None,
         clear_session=lambda: None,
         kite_broker_adapter=SimpleNamespace(
             get_latest_book_snapshot=lambda s: None,
             make_book_snapshot_from_quote=lambda q, s: None))

    _mod("data")
    _mod("data.time_utils",
         now_ist_iso=lambda: S.now.isoformat(),
         now_ist_naive=lambda: S.now,
         today_ist=lambda: S.now.date())
    _mod("data.market_calendar", MarketCalendar=SimpleNamespace(
        is_trading_day=lambda d: S.trading_day,
        get_session_phase=lambda t: SimpleNamespace(value="CONTINUOUS")))
    _mod("data.instrument_resolver", instrument_resolver=SimpleNamespace(
        resolve_token=lambda sym, exchange=None, kite_client=None: S.token_map.get(sym)))

    class HistoricalDataLoader:
        @staticmethod
        def load_cached_data_with_validation(path):
            raise FileNotFoundError(path)

        @staticmethod
        def fetch_real_data(**kw):
            S.fetch_calls.append(kw)
            raise RuntimeError("no real data in test")

    _mod("data.historical_loader", HistoricalDataLoader=HistoricalDataLoader)
    _mod("backtest")
    _mod("backtest.strategy_backtester", StrategyBacktester=object)

    _mod("streaming")

    def get_status():
        if S.stream_raises:
            raise RuntimeError("boom")
        return S.stream_status

    _mod("streaming.market_stream_manager", market_stream_manager=SimpleNamespace(
        get_status=get_status,
        start_stream=lambda token_to_symbol=None: {"subscribed_tokens": 3, "symbols_count": 3},
        stop_stream=lambda: {"status": "stopped"}))

    def get_all_predictions():
        S.predictions_calls += 1
        return S.predictions

    _mod("streaming.live_signal_engine", live_signal_engine=SimpleNamespace(
        get_all_predictions=get_all_predictions))
    _mod("streaming.live_market_state", live_market_state=SimpleNamespace(
        get_all_symbols_state=lambda: S.market_state))

    _mod("strategy")

    def evaluate_symbol(**kw):
        S.eval_calls.append(kw)
        if S.eval_raises:
            raise ValueError("strategy exploded")
        return S.eval_result

    _mod("strategy.prediction_service",
         prediction_service=SimpleNamespace(evaluate_symbol=evaluate_symbol))

    _mod("scanner")
    _mod("scanner.history_context_warmer",
         start_daily_history_warmer=lambda: setattr(S, "warmer_calls", S.warmer_calls + 1))
    _mod("scanner.stock_ranker", StockUniverseScanner=object)

    class KiteConnect:
        def __init__(self, api_key=None): pass
        def login_url(self): return "https://kite.example/login"
    _mod("kiteconnect", KiteConnect=KiteConnect)


_install_stubs()


@pytest.fixture(autouse=True)
def reset_state():
    S.kite = None
    S.auth_err = None
    S.now = datetime(2026, 10, 2, 11, 0, 0)
    S.trading_day = True
    S.stream_status = {"state": "LIVE"}
    S.stream_raises = False
    S.predictions = {}
    S.predictions_calls = 0
    S.warmer_calls = 0
    S.eval_calls = []
    S.eval_result = None
    S.eval_raises = False
    S.universe = []
    S.token_map = {}
    S.market_state = {}
    S.fetch_calls = []
    sys.modules["config.settings"].settings.app_shared_secret = "s3cret"
    sys.modules["config.settings"].settings.instruments[0].instrument_token = None
    sys.modules["config.settings"].settings.instruments[0].max_risk_cap = None
    import backend.signals as sg
    sg._bars_cache.clear()
    yield


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    return TestClient(app)


AUTH = {"X-Shared-Secret": "s3cret"}


class FakeKite:
    def __init__(self, quote=None, bars=None, quote_exc=None, bars_by_range=None):
        self._quote, self._bars, self._quote_exc = quote, bars, quote_exc
        self.hist_calls = 0
        self.quote_calls = []

    def quote(self, keys):
        self.quote_calls.append(list(keys))
        if self._quote_exc:
            raise self._quote_exc
        return self._quote(keys) if callable(self._quote) else (self._quote or {})

    def historical_data(self, instrument_token, from_date, to_date, interval):
        self.hist_calls += 1
        return self._bars(from_date, to_date) if callable(self._bars) else (self._bars or [])
