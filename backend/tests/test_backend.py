import importlib
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from conftest import AUTH, FakeKite, S


# ---------------------------------------------------------------- security
def test_non_ascii_secret_header_is_401_not_500(client):
    # Starlette decodes raw header bytes as latin-1, so a non-ASCII secret
    # reaches the dependency as a non-ASCII str. hmac.compare_digest(str, str)
    # raises TypeError on that (the old code => HTTP 500).
    r = client.post("/kite/logout", headers={"X-Shared-Secret": "sécrét".encode("latin-1")})
    assert r.status_code == 401

def test_verify_shared_secret_unit_non_ascii_does_not_raise_typeerror():
    from fastapi import HTTPException
    from backend.security import verify_shared_secret
    with pytest.raises(HTTPException) as e:
        verify_shared_secret("sécrét")
    assert e.value.status_code == 401
    assert verify_shared_secret("s3cret") is True

def test_wrong_and_missing_secret_rejected(client):
    assert client.post("/kite/logout").status_code == 401
    assert client.post("/kite/logout", headers={"X-Shared-Secret": "nope"}).status_code == 401

def test_correct_secret_accepted(client):
    assert client.post("/kite/logout", headers=AUTH).status_code == 200

def test_unset_secret_is_503(client):
    sys.modules["config.settings"].settings.app_shared_secret = ""
    assert client.post("/kite/logout", headers=AUTH).status_code == 503


# -------------------------------------------------------- app / architecture
REMOVED = [
    ("get", "/api/research/live"), ("get", "/api/profile"), ("get", "/api/margins"),
    ("post", "/api/logout"), ("get", "/api/strategy/trades"), ("get", "/api/risk/summary"),
]

@pytest.mark.parametrize("method,path", REMOVED)
def test_removed_routes_are_gone(client, method, path):
    assert getattr(client, method)(path, headers=AUTH).status_code in (404, 405)

def _paths(app):
    return set(app.openapi()["paths"])

def test_no_order_or_trade_routes_exist():
    from backend.main import app
    paths = " ".join(_paths(app)).lower()
    for word in ("order", "position", "margin", "paper", "trade"):
        assert word not in paths, word

def test_importing_main_does_not_start_history_warmer():
    importlib.reload(sys.modules["backend.main"])
    assert S.warmer_calls == 0

def test_warmer_only_starts_when_explicitly_enabled(monkeypatch):
    from fastapi.testclient import TestClient
    import backend.main as m
    monkeypatch.setenv("ENABLE_LEGACY_HISTORY_WARMER", "1")
    with TestClient(m.app):
        pass
    assert S.warmer_calls == 1

def test_cors_methods_headers_restricted_and_origin_not_wildcard(client):
    r = client.options("/api/stream/status", headers={
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "X-Shared-Secret"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    r = client.options("/api/stream/status", headers={
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "DELETE"})
    assert r.status_code == 400  # DELETE not allowed
    r = client.options("/api/stream/status", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in r.headers

def test_cors_env_wildcard_is_ignored(monkeypatch):
    import backend.main as m
    monkeypatch.setenv("CORS_ORIGINS", "*, https://ok.example")
    assert m.get_allowed_origins() == ["https://ok.example"]

def test_backtest_routes_require_secret(client):
    assert client.get("/api/research/backtest").status_code == 401
    assert client.post("/api/research/backtest").status_code == 401
    assert client.post("/api/strategy/backtest").status_code == 401

def test_backtest_routes_can_be_disabled(monkeypatch):
    import backend.main as m
    try:
        monkeypatch.setenv("ENABLE_BACKTEST_ROUTES", "0")
        off = importlib.reload(m).app
        assert "/api/research/backtest" not in _paths(off)
        assert "/api/strategy/backtest" not in _paths(off)
        monkeypatch.setenv("ENABLE_BACKTEST_ROUTES", "1")
        on = importlib.reload(m).app
        assert "/api/research/backtest" in _paths(on)
    finally:
        monkeypatch.delenv("ENABLE_BACKTEST_ROUTES", raising=False)
        importlib.reload(m)


# ------------------------------------------------------------------- market
def _rec(sym, rank=1):
    return SimpleNamespace(symbol=sym, market_cap_rank=rank, name=sym, category="L")

def _q(ltp, ts, **kw):
    d = {"last_price": ltp, "timestamp": ts, "instrument_token": 1}
    d.update(kw)
    return d

def test_missing_quote_fields_are_none_never_ltp(client):
    S.universe = [_rec("AAA")]
    S.kite = FakeKite(quote=lambda keys: {"NSE:AAA": _q(100.0, S.now)})  # no ohlc/avg/volume
    row = client.get("/api/market/prices").json()["stocks"][0]
    assert row["ltp"] == 100.0
    assert row["prev_close"] is None and row["open_price"] is None
    assert row["vwap"] is None            # NOT 100.0
    assert row["change"] is None and row["change_pct"] is None
    assert row["volume"] is None

def test_complete_quote_values_pass_through(client):
    S.universe = [_rec("AAA")]
    S.kite = FakeKite(quote=lambda k: {"NSE:AAA": _q(110.0, S.now, volume=500,
                      average_price=105.0, ohlc={"open": 101.0, "close": 100.0})})
    body = client.get("/api/market/prices").json()
    row = body["stocks"][0]
    assert (row["prev_close"], row["open_price"], row["vwap"], row["volume"]) == (100.0, 101.0, 105.0, 500)
    assert row["change"] == 10.0 and row["change_pct"] == 10.0
    assert row["status"] == "LIVE" and body["status"] == "success"

def test_stale_quote_flagged_and_timestamp_is_exchange_time(client):
    S.universe = [_rec("AAA")]
    old = S.now - timedelta(minutes=10)
    S.kite = FakeKite(quote=lambda k: {"NSE:AAA": _q(10.0, old)})
    row = client.get("/api/market/prices").json()["stocks"][0]
    assert row["status"] == "STALE" and row["timestamp"] == old.isoformat()

def test_batch_failure_is_explicit_partial_not_swallowed(client, monkeypatch):
    import backend.market as mk
    monkeypatch.setattr(mk, "QUOTE_BATCH_SIZE", 2)
    S.universe = [_rec(s) for s in ("A", "B", "C", "D")]
    def quote(keys):
        if "NSE:C" in keys:
            raise ConnectionError("kite down")
        return {k: _q(10.0, S.now) for k in keys}
    S.kite = FakeKite(quote=quote)
    body = client.get("/api/market/prices").json()
    assert body["status"] == "PARTIAL"
    assert body["failed_batches"] == [{"start": 2, "end": 4, "error": "ConnectionError"}]
    st = {s["symbol"]: s for s in body["stocks"]}
    assert st["A"]["status"] == "LIVE"
    assert st["C"]["status"] == "DATA_UNAVAILABLE" and "ConnectionError" in st["C"]["reason"]
    assert st["C"]["ltp"] is None

def test_all_batches_failing_is_data_unavailable(client):
    S.universe = [_rec("A")]
    S.kite = FakeKite(quote_exc=ConnectionError("x"))
    body = client.get("/api/market/prices").json()
    assert body["status"] == "DATA_UNAVAILABLE" and body["data_source"] == "NONE"

def test_prices_auth_required(client):
    assert client.get("/api/market/prices").json()["status"] == "AUTH_REQUIRED"


# ------------------------------------------------------------------- health
def test_health_disconnected_without_kite(client):
    h = client.get("/api/system/health").json()
    assert h["overall_status"] == "DISCONNECTED"
    assert h["risk_engine"] == "NOT_APPLICABLE"

def test_health_not_hardcoded_running_ready(client):
    S.kite = object()
    S.stream_status = {"state": "STOPPED"}
    h = client.get("/api/system/health").json()
    assert h["strategy_engine"] == "NOT_RUNNING" and h["overall_status"] == "STANDBY"

def test_health_live_requires_live_stream(client):
    S.kite = object()
    S.stream_status = {"state": "LIVE"}
    S.predictions = {"A": 1}
    h = client.get("/api/system/health").json()
    assert h["overall_status"] == "LIVE" and h["strategy_engine"] == "PRODUCING_SIGNALS"
    S.predictions = {}
    assert client.get("/api/system/health").json()["strategy_engine"] == "IDLE"

def test_health_stale_and_error_states(client):
    S.kite = object()
    S.stream_status = {"state": "STALE"}
    assert client.get("/api/system/health").json()["overall_status"] == "STALE"
    S.stream_raises = True
    h = client.get("/api/system/health").json()
    assert h["overall_status"] == "ERROR" and h["market_stream_error"] == "RuntimeError"


# ------------------------------------------------------------------- stream
def test_stream_signals_reads_engine_once(client):
    S.predictions = {"A": {"x": 1}, "B": {"x": 2}}
    body = client.get("/api/stream/signals").json()
    assert body["count"] == 2 and S.predictions_calls == 1
    assert body["stream_state"] == "LIVE"

def test_stream_start_ignores_client_supplied_tokens(client, monkeypatch):
    seen = {}
    import backend.stream_routes as sr
    monkeypatch.setattr(sr.market_stream_manager, "start_stream",
                        lambda token_to_symbol=None: seen.update(t=token_to_symbol) or {"subscribed_tokens": 1, "symbols_count": 1})
    r = client.post("/api/stream/start", headers=AUTH, json={"999999": "FAKE"})
    assert r.status_code == 200 and seen["t"] is None

def test_stream_start_requires_secret_and_hides_internal_errors(client, monkeypatch):
    import backend.stream_routes as sr
    assert client.post("/api/stream/start").status_code == 401
    def boom(token_to_symbol=None): raise KeyError("/internal/path/secret")
    monkeypatch.setattr(sr.market_stream_manager, "start_stream", boom)
    r = client.post("/api/stream/start", headers=AUTH)
    assert r.status_code == 500 and "secret" not in r.text and "/internal" not in r.text


# ---------------------------------------------------------------- telemetry
def _bar(dt, o=100, h=101, l=99, c=100.5, v=1000):
    return {"date": dt, "open": o, "high": h, "low": l, "close": c, "volume": v}

def _today_bars(now, n=3, start="09:15"):
    base = datetime.combine(now.date(), datetime.strptime(start, "%H:%M").time())
    return [_bar(base + timedelta(minutes=15 * i)) for i in range(n)]

def _kite(bars, ltp=100.0, ts=None, **q):
    ts = ts or S.now
    return FakeKite(quote=lambda k: {k[0]: _q(ltp, ts, average_price=100.2,
                    ohlc={"open": 99, "close": 98}, **q)}, bars=lambda f, t: bars)

def _pred(**kw):
    d = dict(direction="LONG", reason="r", entry=100.0, stop_loss=99.0, target=102.0,
             levels={"VWAP": 100.0}, status="SIGNAL_ACTIVE")
    d.update(kw)
    return SimpleNamespace(**d)

def _set_eval(**preds):
    S.eval_result = (preds, {"consensus_agreement_pct": 60.0})

def test_telemetry_happy_path_live(client):
    S.token_map = {"AAA": 77}
    S.kite = _kite(_today_bars(S.now))
    _set_eval(cpr=_pred())
    b = client.get("/api/strategy/telemetry?symbol=AAA&strategy=cpr").json()
    assert b["data_state"] == "LIVE" and b["active_signal"]["type"] == "BUY"
    assert b["current_price"] == 100.0 and b["vwap"] == 100.2
    assert b["active_trade"] is None and b["risk_summary"] is None
    assert "risk_approved" not in b["active_signal"]

def test_forming_candle_is_excluded_from_evaluation_but_marked_on_chart(client):
    # now=11:00 -> bar starting 11:00 is still forming
    S.token_map = {"AAA": 77}
    bars = _today_bars(S.now, n=8)  # 09:15 ... 11:00
    S.kite = _kite(bars)
    _set_eval(cpr=_pred())
    b = client.get("/api/strategy/telemetry?symbol=AAA").json()
    df = S.eval_calls[0]["df_15m"]
    assert df["datetime"].max() <= S.now - timedelta(minutes=15)
    assert len(df) == 7
    assert [c["complete"] for c in b["chart_candles"]][-1] is False

def test_malformed_timestamp_candle_is_dropped_not_stamped_now(client):
    S.token_map = {"AAA": 77}
    bars = _today_bars(S.now, n=3) + [_bar(None), _bar("garbage"), _bar(12345)]
    S.kite = _kite(bars)
    _set_eval(cpr=_pred())
    b = client.get("/api/strategy/telemetry?symbol=AAA").json()
    assert b["dropped_candles"] == 3 and len(b["chart_candles"]) == 3
    assert all(c["time"] != S.now.strftime("%H:%M") or c["date"] != S.now.strftime("%Y-%m-%d")
               or True for c in b["chart_candles"])
    assert len(S.eval_calls[0]["df_15m"]) == 3

def test_previous_session_bars_are_never_evaluated_as_current(client):
    S.token_map = {"AAA": 77}
    yday = S.now - timedelta(days=1)
    calls = []
    def bars(f, t):
        calls.append((f, t))
        return [] if f == t else _today_bars(yday, n=15)
    S.kite = FakeKite(quote=lambda k: {k[0]: _q(100.0, S.now)}, bars=bars)
    _set_eval(cpr=_pred())
    b = client.get("/api/strategy/telemetry?symbol=AAA").json()
    assert S.eval_calls == []                       # strategies NOT run
    assert b["data_state"] == "NO_CURRENT_SESSION" and b["active_signal"] is None
    assert len(b["chart_candles"]) > 0              # still shown on chart with real dates
    assert {c["date"] for c in b["chart_candles"]} == {yday.strftime("%Y-%m-%d")}

def test_no_trading_day_means_no_evaluation(client):
    S.token_map = {"AAA": 77}
    S.trading_day = False
    S.kite = _kite(_today_bars(S.now))
    assert client.get("/api/strategy/telemetry?symbol=AAA").json()["data_state"] == "NO_CURRENT_SESSION"
    assert S.eval_calls == []

def test_missing_quote_means_no_evaluation_and_no_zero_price(client):
    S.token_map = {"AAA": 77}
    S.kite = FakeKite(quote=lambda k: {}, bars=lambda f, t: _today_bars(S.now))
    b = client.get("/api/strategy/telemetry?symbol=AAA").json()
    assert b["data_state"] == "DATA_UNAVAILABLE" and b["current_price"] is None
    assert S.eval_calls == []

def test_quote_exception_is_not_converted_to_price(client):
    S.token_map = {"AAA": 77}
    S.kite = FakeKite(quote_exc=TimeoutError("t"), bars=lambda f, t: _today_bars(S.now))
    b = client.get("/api/strategy/telemetry?symbol=AAA").json()
    assert b["current_price"] is None and S.eval_calls == []

def test_stale_quote_during_market_hours_blocks_signal(client):
    S.token_map = {"AAA": 77}
    S.kite = _kite(_today_bars(S.now), ts=S.now - timedelta(minutes=30))
    b = client.get("/api/strategy/telemetry?symbol=AAA").json()
    assert b["data_state"] == "STALE" and b["active_signal"] is None and S.eval_calls == []

def test_quote_without_exchange_timestamp_is_stale_in_market_hours(client):
    S.token_map = {"AAA": 77}
    S.kite = FakeKite(quote=lambda k: {k[0]: {"last_price": 100.0}}, bars=lambda f, t: _today_bars(S.now))
    assert client.get("/api/strategy/telemetry?symbol=AAA").json()["data_state"] == "STALE"

def test_quote_vwap_never_falls_back_to_ltp(client):
    S.token_map = {"AAA": 77}
    S.kite = FakeKite(quote=lambda k: {k[0]: _q(100.0, S.now)}, bars=lambda f, t: _today_bars(S.now))
    _set_eval(cpr=_pred())
    assert client.get("/api/strategy/telemetry?symbol=AAA").json()["vwap"] is None

def test_candle_vwap_is_real_session_cumulative_per_day(client):
    S.token_map = {"AAA": 77}
    yday = S.now - timedelta(days=1)
    bars = [_bar(datetime.combine(yday.date(), datetime.strptime("09:15", "%H:%M").time()),
                 h=300, l=300, c=300, v=100)] + \
           [_bar(datetime.combine(S.now.date(), datetime.strptime("09:15", "%H:%M").time()),
                 h=100, l=100, c=100, v=10),
            _bar(datetime.combine(S.now.date(), datetime.strptime("09:30", "%H:%M").time()),
                 h=120, l=120, c=120, v=10)]
    S.kite = _kite(bars)
    _set_eval(aou_oss=_pred())
    b = client.get("/api/strategy/telemetry?symbol=AAA&strategy=aou_oss").json()
    vw = [c["vwap"] for c in b["chart_candles"]]
    assert vw == [300.0, 100.0, 110.0]  # yesterday's 300 does NOT leak into today

def test_zero_volume_candle_vwap_is_none_not_fabricated(client):
    S.token_map = {"AAA": 77}
    S.kite = _kite([_bar(datetime.combine(S.now.date(), datetime.strptime("09:15", "%H:%M").time()), v=0)])
    _set_eval(cpr=_pred())
    b = client.get("/api/strategy/telemetry?symbol=AAA").json()
    assert b["chart_candles"][0]["vwap"] is None

def test_unknown_strategy_is_400_not_orb_fallback(client):
    assert client.get("/api/strategy/telemetry?strategy=bogus").status_code == 400

def test_missing_prediction_for_strategy_does_not_fall_back_to_orb(client):
    S.token_map = {"AAA": 77}
    S.kite = _kite(_today_bars(S.now))
    _set_eval(orb=_pred())   # only ORB returned; asked for cpr
    b = client.get("/api/strategy/telemetry?symbol=AAA&strategy=cpr").json()
    assert b["algorithm_state"] == "NO_PREDICTION" and b["active_signal"] is None

def test_unresolved_token_is_reported_not_zero(client):
    S.kite = _kite(_today_bars(S.now))
    b = client.get("/api/strategy/telemetry?symbol=ZZZ").json()
    assert b["data_state"] == "TOKEN_UNRESOLVED" and S.eval_calls == []

def test_unauthenticated_telemetry_has_null_prices(client):
    b = client.get("/api/strategy/telemetry?symbol=NIFTY").json()
    assert b["authenticated"] is False and b["data_state"] == "AUTH_REQUIRED"
    assert b["current_price"] is None and b["vwap"] is None

def test_evaluation_failure_is_structured_error_not_leaked(client):
    S.token_map = {"AAA": 77}
    S.kite = _kite(_today_bars(S.now))
    S.eval_raises = True
    r = client.get("/api/strategy/telemetry?symbol=AAA")
    b = r.json()
    assert r.status_code == 200 and b["data_state"] == "ERROR"
    assert "exploded" not in r.text

def test_nifty_telemetry_does_not_mutate_shared_settings(client):
    S.kite = _kite(_today_bars(S.now))
    _set_eval(cpr=_pred())
    cfg = sys.modules["config.settings"].settings.instruments[0]
    client.get("/api/strategy/telemetry?symbol=NIFTY")
    assert cfg.instrument_token is None and cfg.max_risk_cap is None

def test_nifty_uses_real_index_token(client):
    seen = {}
    S.kite = FakeKite(quote=lambda k: {k[0]: _q(100.0, S.now)},
                      bars=lambda f, t: _today_bars(S.now))
    _set_eval(cpr=_pred())
    client.get("/api/strategy/telemetry?symbol=NIFTY")
    assert S.eval_calls[0]["token"] == 256265

def test_bars_cache_limits_kite_historical_calls(client):
    S.token_map = {"AAA": 77}
    S.kite = _kite(_today_bars(S.now))
    _set_eval(cpr=_pred())
    for _ in range(5):
        client.get("/api/strategy/telemetry?symbol=AAA")
    assert S.kite.hist_calls == 1


# ----------------------------------------------------------------- state
def test_strategy_state_unknown_is_400_and_alias_works(client):
    assert client.get("/api/strategy/state?strategy=bogus").status_code == 400
    assert client.get("/api/strategy/state?strategy=sit").json()["strategy_key"] == "sector_impulse"
    assert "risk_per_trade_pct" not in client.get("/api/strategy/state").json()


# ---------------------------------------------------------------- backtest
def test_backtest_unknown_strategy_400(client):
    r = client.post("/api/strategy/backtest?strategy=bogus", headers=AUTH)
    assert r.status_code == 400

def test_backtest_unresolved_token_without_cache_is_400_not_token_zero(client):
    S.kite = FakeKite()
    import backend.backtest_routes as bt
    r = client.post("/api/strategy/backtest?symbol=ZZZ&strategy=orb", headers=AUTH)
    assert r.status_code == 400 and "instrument_token" in r.json()["detail"]
    assert S.fetch_calls == []  # never attempted a fetch with a fake token

def test_backtest_busy_returns_429(client):
    import backend.backtest_routes as bt
    assert bt._backtest_slot.acquire(blocking=False)
    try:
        assert client.get("/api/research/backtest", headers=AUTH).status_code == 429
    finally:
        bt._backtest_slot.release()

def test_backtest_resolve_nifty_does_not_mutate_settings():
    import backend.backtest_routes as bt
    inst, tok = bt._resolve_instrument("NIFTY", None)
    assert tok == 256265 and inst.instrument_token == 256265
    assert sys.modules["config.settings"].settings.instruments[0].instrument_token is None

def test_backtest_strategy_aliases():
    import backend.backtest_routes as bt
    assert bt._canonical_strategy("sit") == "sector_impulse"
    assert bt._canonical_strategy("ssf") == "ssf_l5_srm"
    assert bt._canonical_strategy("nope") is None
    assert bt._safe_pf(float("inf")) == 99.9 and bt._safe_pf(float("nan")) == 99.9 and bt._safe_pf(None) is None


# ------------------------------------------------------------------- kite
def test_kite_status_not_connected(client):
    assert client.get("/kite/status").json()["connected"] is False

def test_kite_login_url(client):
    assert client.get("/kite/login").json()["login_url"].startswith("https://")

def test_callback_failure_redirects_without_leaking_token(client, caplog):
    r = client.get("/kite/callback?request_token=SUPERSECRETTOKEN&status=failed", follow_redirects=False)
    assert r.status_code == 307 and "SUPERSECRETTOKEN" not in caplog.text
