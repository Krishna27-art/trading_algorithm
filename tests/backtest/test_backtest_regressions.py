"""
Regression tests for backtest-path defects.

Fixture candles here only exercise plumbing (calendar, validation, seeding,
P&L bookkeeping); they make no claim about strategy performance.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.data import historical_loader as hl
from backend.data.historical_loader import HistoricalDataLoader, exclude_irregular_sessions
from backend.data.time_utils import MarketCalendar


def _session(day: date, n: int = 25, base: float = 100.0) -> pd.DataFrame:
    t = datetime.combine(day, time(9, 15))
    rows = []
    for i in range(n):
        o = base + i * 0.1
        rows.append({
            "datetime": t + timedelta(minutes=15 * i),
            "open": o, "high": o + 0.5, "low": o - 0.5, "close": o + 0.2, "volume": 1000,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Weekend special sessions (the reported "Weekend candles detected" failure)
# ---------------------------------------------------------------------------

def test_budget_sunday_session_is_valid_real_data():
    df = pd.concat([_session(date(2026, 1, 30)), _session(date(2026, 2, 1)), _session(date(2026, 2, 2))],
                   ignore_index=True)
    ok, errors = HistoricalDataLoader.validate_candles(df)
    assert ok, errors


def test_unexplained_weekend_candles_are_still_rejected():
    df = pd.concat([_session(date(2026, 2, 6)), _session(date(2026, 2, 7))], ignore_index=True)
    ok, errors = HistoricalDataLoader.validate_candles(df)
    assert not ok
    assert any("2026-02-07" in e for e in errors)


def test_calendar_treats_budget_sunday_as_trading_day():
    assert MarketCalendar.is_trading_day(date(2026, 2, 1))
    assert MarketCalendar.previous_trading_session(date(2026, 2, 2)) == date(2026, 2, 1)
    assert not MarketCalendar.is_trading_day(date(2026, 2, 7))


def test_irregular_sessions_are_excluded_from_backtest_data():
    df = pd.concat([
        _session(date(2024, 5, 17)),
        _session(date(2024, 5, 18), n=4),     # DR-drill Saturday (partial)
        _session(date(2025, 10, 21), n=4),    # Muhurat on an exchange holiday
        _session(date(2026, 2, 1)),           # Budget Sunday (full) - kept
    ], ignore_index=True)
    out, dropped = exclude_irregular_sessions(df)
    assert dropped == [date(2024, 5, 18), date(2025, 10, 21)]
    kept = set(out["datetime"].dt.date)
    assert kept == {date(2024, 5, 17), date(2026, 2, 1)}
    assert len(out) == 50  # rows removed only, none created


# ---------------------------------------------------------------------------
# Forming candle must not be cached / backtested
# ---------------------------------------------------------------------------

class _FakeKite:
    def __init__(self, rows):
        self.rows = rows

    def historical_data(self, instrument_token, from_date, to_date, interval):
        return [dict(r) for r in self.rows]


def test_fetch_real_data_drops_forming_candle(monkeypatch, tmp_path):
    now = datetime(2026, 10, 28, 11, 5, 0)
    monkeypatch.setattr(hl, "now_ist_naive", lambda: now)
    rows = []
    for i, start in enumerate([time(10, 30), time(10, 45), time(11, 0)]):  # 11:00 bar is forming
        o = 100.0 + i
        rows.append({"date": datetime.combine(now.date(), start), "open": o, "high": o + 1,
                     "low": o - 1, "close": o + 0.5, "volume": 10})
    df = HistoricalDataLoader.fetch_real_data(
        kite_client=_FakeKite(rows), instrument_token=1,
        start_date=now.date(), end_date=now.date(), interval="15minute",
        cache_path=None, request_pause_seconds=0,
    )
    assert list(df["datetime"].dt.time) == [time(10, 30), time(10, 45)]


# ---------------------------------------------------------------------------
# Replay must not crash on an EXIT signal
# ---------------------------------------------------------------------------

def test_prediction_replay_handles_exit_signal():
    from backend.strategy.base_strategy import SignalAction, StrategySignal
    from backend.strategy.prediction_service import PredictionService

    seq = iter([SignalAction.BUY, SignalAction.EXIT])

    class Stub:
        def on_candle(self, candle, vwap):
            return StrategySignal(action=next(seq), symbol="X", timestamp=candle["datetime"],
                                  price=candle["close"])

    rows = _session(date(2026, 10, 27), n=2)
    rows["vwap"] = rows["close"]
    assert PredictionService._replay_rows(Stub(), rows) is None


# ---------------------------------------------------------------------------
# Walk-forward seeding: each session sees the sessions before it
# ---------------------------------------------------------------------------

def test_pretrain_backtest_seeds_each_session_with_prior_day():
    from backend.backtest.strategy_backtester import StrategyBacktester
    from backend.config.universe import create_instrument_config_for_equity

    seen = {}

    class Recorder:
        position = 0

        def seed_context(self, df):
            self._last = pd.to_datetime(df["datetime"]).dt.date.max() if len(df) else None

        def reset_session(self, d):
            seen[d] = self._last

        def on_candle(self, candle, vwap):
            return None

    train = pd.concat([_session(date(2026, 10, 19)), _session(date(2026, 10, 21))], ignore_index=True)
    test = pd.concat([_session(date(2026, 10, 22)), _session(date(2026, 10, 23))], ignore_index=True)
    inst = create_instrument_config_for_equity("TESTCO", 1, current_price=100.0, lot_size=1)
    StrategyBacktester(lambda: Recorder(), inst).generate_trades(test, pretrain_df=train)

    assert seen[date(2026, 10, 22)] == date(2026, 10, 21)
    assert seen[date(2026, 10, 23)] == date(2026, 10, 22)  # was frozen at 10-21 before the fix


# ---------------------------------------------------------------------------
# Pair (CRSD) trade records feed PerformanceAnalyzer; hedge exit is real
# ---------------------------------------------------------------------------

def test_pair_trade_record_is_reportable_and_uses_real_hedge_exit():
    from backend.backtest.pair_backtester import PairBacktester
    from backend.backtest.performance import PerformanceAnalyzer
    from backend.config.universe import create_instrument_config_for_equity
    from backend.strategy.base_strategy import SignalAction, StrategySignal
    from backend.strategy.crsd_strategy import CRSDPeerContext

    day = date(2026, 10, 27)
    peer = _session(day, n=4, base=200.0)               # bars 09:15..10:00
    ctx = CRSDPeerContext(peers={"PEER": peer}, market=_session(day, n=4, base=25000.0))
    inst = create_instrument_config_for_equity("TESTCO", 1, current_price=100.0, lot_size=1)
    bt = PairBacktester(strategy_factory=lambda: None, instrument=inst, peer_context=ctx)

    entry_ts = datetime.combine(day, time(9, 15))
    exit_ts = datetime.combine(day, time(10, 30))        # no exact peer bar at exit
    trade = {
        "entry_timestamp": entry_ts, "target_symbol": "TESTCO", "target_action": "BUY",
        "target_entry_price": 100.0, "target_quantity": 10, "target_stop_loss": 99.0,
        "target_target": 102.0, "hedge_quantities": {"PEER": 5}, "hedge_actions": {"PEER": "SELL"},
        "hedge_entry_prices": {"PEER": 200.2}, "entry_reason": "test", "risk_scale": 1.0,
    }
    exit_sig = StrategySignal(action=SignalAction.EXIT, symbol="TESTCO", timestamp=exit_ts, price=101.0)
    rec = bt._close_pair_trade(trade, exit_sig, exit_ts, 101.0)

    last_real = float(peer["close"].iloc[-1])
    assert rec["hedge_exit_prices"]["PEER"] == last_real
    assert rec["hedge_exit_price_source"]["PEER"] == "LAST_REAL_BAR"
    assert rec["hedge_exit_prices"]["PEER"] != trade["hedge_entry_prices"]["PEER"]

    report = PerformanceAnalyzer.generate_report([dict(rec, entry_time=entry_ts)])
    assert report.total_trades == 1
    assert report.net_pnl == pytest.approx(rec["net_pnl"])


# ---------------------------------------------------------------------------
# Route: no-sector symbols are reported, not faked
# ---------------------------------------------------------------------------

def test_sector_context_for_index_is_not_applicable():
    from backend.routes.backtest import StrategyUnavailable, _backtest_sector_context

    inst = SimpleNamespace(symbol="NIFTY")
    with pytest.raises(StrategyUnavailable) as exc:
        _backtest_sector_context(inst, 30, kite=None, auth_err=None)
    assert exc.value.state == "NOT_APPLICABLE"
