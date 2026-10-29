"""
Tests for Monte Carlo, the ORB Retest research strategy, walk-forward route
wiring and holiday-calendar coverage. Fixture candles only exercise logic.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from backend.backtest.monte_carlo import MIN_TRADES, run_monte_carlo
from backend.config.universe import create_instrument_config_for_equity
from backend.data.time_utils import MarketCalendar
from backend.strategy.base_strategy import SignalAction
from backend.strategy.orb_retest_strategy import ORBRetestConfig, ORBRetestStrategy


# ---------------------------------------------------------------- Monte Carlo

def test_monte_carlo_flags_small_samples():
    res = run_monte_carlo([{"pnl_net": 100.0}] * 5, simulations=200)
    assert res.trades == 5 and res.reliable is False


def test_monte_carlo_shuffle_keeps_total_and_finds_drawdown():
    trades = [{"pnl_net": 1000.0}] * 30 + [{"pnl_net": -800.0}] * 20
    res = run_monte_carlo(trades, initial_capital=100_000.0, simulations=500, seed=1)
    assert res.reliable is True and res.trades == 50
    assert res.max_drawdown_pct_p95 >= res.max_drawdown_pct_p50 > 0
    assert res.losing_streak_p95 >= 3
    assert res.net_pnl_p05 <= res.net_pnl_p50 <= res.net_pnl_p95
    assert 0.0 <= res.probability_of_loss_pct <= 100.0


def test_monte_carlo_reads_pair_trade_key_and_is_deterministic():
    trades = [{"net_pnl": float(v)} for v in range(-20, 30)]
    a = run_monte_carlo(trades, simulations=300, seed=3).to_dict()
    b = run_monte_carlo(trades, simulations=300, seed=3).to_dict()
    assert a == b and a["trades"] == 50


def test_monte_carlo_no_trades():
    res = run_monte_carlo([])
    assert res.trades == 0 and res.net_pnl_p50 is None


# ---------------------------------------------------------------- ORB Retest

def _history(n_days: int, end: date, vol: int = 1000) -> pd.DataFrame:
    rows, d = [], end
    days = []
    while len(days) < n_days:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    for day in sorted(days):
        t = datetime.combine(day, time(9, 15))
        for i in range(25):
            rows.append({"datetime": t + timedelta(minutes=15 * i), "open": 100.0, "high": 101.0,
                         "low": 99.0, "close": 100.0, "volume": vol})
    return pd.DataFrame(rows)


def _bar(day, hhmm, o, h, l, c, v):
    hh, mm = map(int, hhmm.split(":"))
    return {"datetime": datetime.combine(day, time(hh, mm)), "open": o, "high": h, "low": l, "close": c, "volume": v}


def _strategy():
    inst = create_instrument_config_for_equity("TESTCO", 1, current_price=100.0, lot_size=1)
    return ORBRetestStrategy(inst, params=ORBRetestConfig())


DAY = date(2026, 10, 27)


def test_orb_retest_long_on_retest_after_rvol_breakout():
    s = _strategy()
    s.seed_context(_history(25, DAY))
    s.reset_session(DAY)
    assert s.daily_atr == pytest.approx(2.0)
    assert s.on_candle(_bar(DAY, "09:15", 100, 100.5, 99.8, 100.2, 1000), 100) is None  # range 99.8-100.5
    assert s.on_candle(_bar(DAY, "09:30", 100.2, 101.0, 100.1, 100.9, 2000), 100) is None  # close-break, RVOL 2.0 -> armed
    assert s._armed is not None and s._armed["side"] == 1
    sig = s.on_candle(_bar(DAY, "09:45", 100.9, 101.0, 100.4, 100.8, 1200), 100)  # retests 100.5, closes above
    assert sig is not None and sig.action == SignalAction.BUY
    assert sig.stop_loss < 100.4 and sig.target > sig.price


def test_orb_retest_low_rvol_breakout_is_ignored():
    s = _strategy()
    s.seed_context(_history(25, DAY))
    s.reset_session(DAY)
    s.on_candle(_bar(DAY, "09:15", 100, 100.5, 99.8, 100.2, 1000), 100)
    s.on_candle(_bar(DAY, "09:30", 100.2, 101.0, 100.1, 100.9, 900), 100)   # RVOL 0.9
    assert s._armed is None and "RVOL" in s.last_reason
    assert s.on_candle(_bar(DAY, "09:45", 100.9, 101.0, 100.4, 100.8, 1200), 100) is None


def test_orb_retest_without_history_never_trades():
    s = _strategy()
    s.reset_session(DAY)
    s.on_candle(_bar(DAY, "09:15", 100, 100.5, 99.8, 100.2, 1000), 100)
    assert s.on_candle(_bar(DAY, "09:30", 100.2, 101.0, 100.1, 100.9, 5000), 100) is None
    assert "ATR" in s.last_reason


def test_orb_retest_index_without_volume_never_trades():
    s = _strategy()
    s.seed_context(_history(25, DAY, vol=0))
    s.reset_session(DAY)
    s.on_candle(_bar(DAY, "09:15", 100, 100.5, 99.8, 100.2, 0), 100)
    assert s.on_candle(_bar(DAY, "09:30", 100.2, 101.0, 100.1, 100.9, 0), 100) is None
    assert "RVOL" in s.last_reason


def test_orb_retest_failed_breakout_disarms():
    s = _strategy()
    s.seed_context(_history(25, DAY))
    s.reset_session(DAY)
    s.on_candle(_bar(DAY, "09:15", 100, 100.5, 99.8, 100.2, 1000), 100)
    s.on_candle(_bar(DAY, "09:30", 100.2, 101.0, 100.1, 100.9, 2000), 100)
    s.on_candle(_bar(DAY, "09:45", 100.9, 100.9, 99.5, 99.6, 1500), 100)    # closes back below range
    assert s._armed is None and "failed" in s.last_reason


# ---------------------------------------------------------------- Walk-forward route

def test_walk_forward_route_runs_on_loaded_history(monkeypatch):
    import backend.routes.backtest as bt

    hist = _history(45, date(2026, 10, 28))
    monkeypatch.setattr(bt, "get_active_kite_with_diagnostics", lambda force_validate=False: (None, "no kite"))
    inst = create_instrument_config_for_equity("TESTCO", 1, current_price=100.0, lot_size=1)
    monkeypatch.setattr(bt, "_resolve_instrument", lambda symbol, kite, instrument_type=None: (inst, 1))
    monkeypatch.setattr(bt, "_load_history", lambda *a, **k: hist.copy())

    out = bt._walk_forward_one(days=60, symbol="TESTCO", key="orb", train_days=20, test_days=10)
    assert out["success"] is True
    assert out["fill_policy"] == "NEXT_BAR_OPEN"
    assert len(out["folds"]) >= 2
    assert "monte_carlo" in out["robustness"]


# ---------------------------------------------------------------- Calendar coverage

def test_calendar_coverage_warns_before_expiry():
    end = MarketCalendar.coverage_end()
    assert MarketCalendar.coverage_status(end - timedelta(days=200))["status"] == "OK"
    expiring = MarketCalendar.coverage_status(end - timedelta(days=10))
    assert expiring["status"] == "EXPIRING" and str(end.year + 1) in expiring["action"]
    assert MarketCalendar.coverage_status(end + timedelta(days=1))["status"] == "EXPIRED"
