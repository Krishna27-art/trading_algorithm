"""
Unit tests for the SQLite Signal Journal and Strategy Performance system.
"""

from datetime import datetime, timedelta
import pytest

from backend.database.db import (
    DatabaseManager,
    SignalEventRecord,
    SignalOutcome,
    SignalStatus,
)
from backend.main import app


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_signals.db"
    return DatabaseManager(db_file)


def test_record_signal_and_deduplication(temp_db):
    sig = SignalEventRecord(
        signal_id="SIG_ORB_RELIANCE_20261007_101500",
        trading_date="2026-10-07",
        symbol="RELIANCE",
        instrument_token=738561,
        strategy="orb",
        direction="LONG",
        generated_at=datetime(2026, 10, 7, 10, 15),
        candle_timestamp=datetime(2026, 10, 7, 10, 0),
        entry_price=1245.30,
        stop_loss=1238.00,
        target=1260.00,
        notes="ORB Breakout",
    )

    # 1. First record should succeed
    assert temp_db.record_signal(sig) is True

    # 2. Duplicate exact ID rejected
    assert temp_db.record_signal(sig) is False

    # 3. Same strategy & symbol active on same day rejected (polling deduplication)
    sig2 = SignalEventRecord(
        signal_id="SIG_ORB_RELIANCE_20261007_103000",
        trading_date="2026-10-07",
        symbol="RELIANCE",
        strategy="orb",
        direction="LONG",
        generated_at=datetime(2026, 10, 7, 10, 30),
        candle_timestamp=datetime(2026, 10, 7, 10, 15),
        entry_price=1245.30,
        stop_loss=1238.00,
        target=1260.00,
    )
    assert temp_db.record_signal(sig2) is False

    # 4. Different strategy (e.g. CPR) on same stock is allowed
    sig_cpr = SignalEventRecord(
        signal_id="SIG_CPR_RELIANCE_20261007_101500",
        trading_date="2026-10-07",
        symbol="RELIANCE",
        strategy="cpr",
        direction="LONG",
        generated_at=datetime(2026, 10, 7, 10, 15),
        entry_price=1245.30,
        stop_loss=1238.00,
        target=1260.00,
    )
    assert temp_db.record_signal(sig_cpr) is True


def test_live_tick_outcome_and_excursion_metrics(temp_db):
    sig = SignalEventRecord(
        signal_id="SIG_ORB_TCS_LONG",
        trading_date="2026-10-07",
        symbol="TCS",
        strategy="orb",
        direction="LONG",
        generated_at=datetime(2026, 10, 7, 10, 0),
        entry_price=3500.0,
        stop_loss=3480.0,
        target=3540.0,
    )
    assert temp_db.record_signal(sig) is True

    # Tick 1: Small favorable move (3510)
    resolved = temp_db.update_active_signal_tick("TCS", 3510.0, datetime(2026, 10, 7, 10, 5))
    assert len(resolved) == 0

    # Tick 2: Small adverse excursion (3490)
    resolved = temp_db.update_active_signal_tick("TCS", 3490.0, datetime(2026, 10, 7, 10, 10))
    assert len(resolved) == 0

    # Tick 3: Target breach (3542) -> WIN
    resolved = temp_db.update_active_signal_tick("TCS", 3542.0, datetime(2026, 10, 7, 10, 25))
    assert len(resolved) == 1
    res = resolved[0]
    assert res["outcome"] == "WIN"
    assert res["signal_status"] == "CLOSED"
    assert res["outcome_price"] == 3542.0
    assert res["mfe_pct"] == round(((3542.0 - 3500.0) / 3500.0) * 100, 2)
    assert res["mae_pct"] == round(((3490.0 - 3500.0) / 3500.0) * 100, 2)
    assert res["return_pct"] == round(((3542.0 - 3500.0) / 3500.0) * 100, 2)
    assert res["minutes_to_outcome"] == 25.0


def test_short_signal_stop_loss_hit(temp_db):
    sig = SignalEventRecord(
        signal_id="SIG_DUALEMA_INFY_SHORT",
        trading_date="2026-10-07",
        symbol="INFY",
        strategy="dual_ema",
        direction="SHORT",
        generated_at=datetime(2026, 10, 7, 11, 0),
        entry_price=1500.0,
        stop_loss=1520.0,
        target=1460.0,
    )
    assert temp_db.record_signal(sig) is True

    # Price moves against short to 1522 -> LOSS
    resolved = temp_db.update_active_signal_tick("INFY", 1522.0, datetime(2026, 10, 7, 11, 15))
    assert len(resolved) == 1
    assert resolved[0]["outcome"] == "LOSS"
    assert resolved[0]["return_pct"] < 0


def test_candle_ambiguity_rule(temp_db):
    sig = SignalEventRecord(
        signal_id="SIG_CPR_HDFC_LONG",
        trading_date="2026-10-07",
        symbol="HDFCBANK",
        strategy="cpr",
        direction="LONG",
        generated_at=datetime(2026, 10, 7, 10, 0),
        entry_price=100.0,
        stop_loss=98.0,
        target=104.0,
    )
    assert temp_db.record_signal(sig) is True

    # Completed candle breaches both target (105 >= 104) and stop (97 <= 98)
    candle = {
        "datetime": datetime(2026, 10, 7, 10, 15),
        "open": 100.0,
        "high": 105.0,
        "low": 97.0,
        "close": 101.0,
    }
    resolved = temp_db.update_active_signals_candle("HDFCBANK", candle)
    assert len(resolved) == 1
    assert resolved[0]["outcome"] == "AMBIGUOUS"
    assert resolved[0]["signal_status"] == "CLOSED"


def test_session_finalization_expired(temp_db):
    sig = SignalEventRecord(
        signal_id="SIG_APEX_WIPRO_LONG",
        trading_date="2026-10-07",
        symbol="WIPRO",
        strategy="apex",
        direction="LONG",
        generated_at=datetime(2026, 10, 7, 11, 0),
        entry_price=500.0,
        stop_loss=490.0,
        target=520.0,
    )
    assert temp_db.record_signal(sig) is True

    # At 15:30 IST session end, neither target nor stop hit
    prices = {"WIPRO": 505.0}
    finalized_count = temp_db.finalize_session_signals(
        trading_date="2026-10-07",
        session_close_time=datetime(2026, 10, 7, 15, 30),
        current_prices=prices,
    )
    assert finalized_count == 1

    # Idempotent: second call should finalize 0
    assert temp_db.finalize_session_signals(trading_date="2026-10-07") == 0

    history = temp_db.get_signal_history(symbol="WIPRO")
    assert len(history) == 1
    assert history[0]["outcome"] == "EXPIRED"
    assert history[0]["outcome_price"] == 505.0
    assert history[0]["return_pct"] == 1.0


def test_strategy_performance_dynamic_aggregation(temp_db):
    # Insert 4 ORB signals: 2 Wins, 1 Loss, 1 Expired
    # Accuracy should be 2 / (2 + 1) = 66.7%, Expired not forced into Loss!
    temp_db.record_signal(SignalEventRecord(
        signal_id="S1", trading_date="2026-10-07", symbol="SBIN", strategy="orb", direction="LONG",
        generated_at=datetime(2026, 10, 7, 9, 30), entry_price=800.0, stop_loss=790.0, target=820.0
    ))
    temp_db.update_active_signal_tick("SBIN", 821.0, datetime(2026, 10, 7, 9, 45))  # WIN

    temp_db.record_signal(SignalEventRecord(
        signal_id="S2", trading_date="2026-10-07", symbol="SBIN", strategy="orb", direction="SHORT",
        generated_at=datetime(2026, 10, 7, 10, 0), entry_price=820.0, stop_loss=830.0, target=800.0
    ))
    temp_db.update_active_signal_tick("SBIN", 799.0, datetime(2026, 10, 7, 10, 30))  # WIN

    temp_db.record_signal(SignalEventRecord(
        signal_id="S3", trading_date="2026-10-07", symbol="RELIANCE", strategy="orb", direction="LONG",
        generated_at=datetime(2026, 10, 7, 11, 0), entry_price=1000.0, stop_loss=980.0, target=1040.0
    ))
    temp_db.update_active_signal_tick("RELIANCE", 979.0, datetime(2026, 10, 7, 11, 20))  # LOSS

    temp_db.record_signal(SignalEventRecord(
        signal_id="S4", trading_date="2026-10-07", symbol="TCS", strategy="orb", direction="LONG",
        generated_at=datetime(2026, 10, 7, 12, 0), entry_price=3000.0, stop_loss=2950.0, target=3100.0
    ))
    temp_db.finalize_session_signals("2026-10-07", current_prices={"TCS": 3020.0})  # EXPIRED

    perf = temp_db.get_strategy_performance("TODAY", strategy="orb")
    assert perf["total_signals"] == 4
    assert perf["total_wins"] == 2
    assert perf["total_losses"] == 1
    assert perf["total_expired"] == 1
    assert perf["overall_accuracy"] == 66.7

    orb_stat = perf["strategies"][0]
    assert orb_stat["strategy"] == "orb"
    assert orb_stat["wins"] == 2
    assert orb_stat["losses"] == 1
    assert orb_stat["expired"] == 1
    assert orb_stat["accuracy"] == 66.7
    assert len(orb_stat["symbol_breakdown"]) == 3  # SBIN, RELIANCE, TCS


def test_api_performance_endpoints():
    from backend.routes.signals import get_strategy_performance, get_signal_journal

    # 1. Performance endpoint
    data = get_strategy_performance(period="TODAY")
    assert data["status"] == "success"
    assert "strategies" in data
    assert "period" in data

    # 2. Journal endpoint
    data_j = get_signal_journal(limit=20)
    assert data_j["status"] == "success"
    assert "signals" in data_j

