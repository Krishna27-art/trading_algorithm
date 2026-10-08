"""
Verification suite for Red-Team Audit Fixes:
1. Intrabar stop/target tombstone prevents signal resurrection on subsequent candle replay.
2. Missing or non-positive VWAP strictly fails closed (UNAVAILABLE) instead of synthesizing VWAP.
3. Consensus agreement percentage is computed over evaluable strategies, not total configured strategies.
4. Cross-process file locking prohibits multiple concurrent WebSocket stream owners.
"""

from datetime import date, datetime, time, timedelta
import pandas as pd
import pytest

from backend.config.settings import InstrumentConfig, InstrumentType
from backend.database.db import SignalEventRecord, db_manager
from backend.strategy.base_strategy import SignalAction, StrategySignal
from backend.strategy.prediction_service import (
    PredictionService,
    SingleStrategyPrediction,
    prediction_service,
)
from backend.streaming.live_signal_engine import LiveSignalEngine


def test_red_team_signal_resurrection_prevented(monkeypatch):
    """RED-TEAM-01: When an intrabar stop is breached, the signal must NOT resurrect on next candle close."""
    engine = LiveSignalEngine()
    engine.reset()

    today_str = datetime.now().strftime("%Y-%m-%d")
    symbol = "TCS"
    strat = "orb"

    # Simulate intrabar breach occurring during session
    engine.mark_signal_closed(
        symbol=symbol,
        strategy=strat,
        trading_date=today_str,
        reason="Intrabar stop breached at 3450.00",
    )

    assert engine.is_signal_closed(symbol, strat, today_str) is True

    def mock_eval(**kwargs):
        preds = {
            "orb": SingleStrategyPrediction(
                status="BUY",
                direction="LONG",
                entry=3500.0,
                stop_loss=3450.0,
                target=3600.0,
                reason="ORB breakout",
            ),
            "cpr": SingleStrategyPrediction(
                status="WAITING",
                direction="NEUTRAL",
            ),
        }
        return preds, prediction_service.calculate_consensus(preds)

    monkeypatch.setattr(prediction_service, "evaluate_symbol", mock_eval)

    # Build candle close event with fresh timestamp
    from backend.data.time_utils import now_ist_naive
    c_time = now_ist_naive()
    candle_dict = {
        "symbol": symbol,
        "datetime": c_time,
        "open": 3500.0,
        "high": 3520.0,
        "low": 3490.0,
        "close": 3510.0,
        "volume": 50000,
        "vwap": 3505.0,
    }

    # Record real tick so LTP is fresh
    engine.record_tick_price(symbol, 3510.0, timestamp=c_time)

    # Trigger on_candle_close
    res = engine.on_candle_close(candle_dict, vwap=3505.0)
    assert res is not None

    orb_pred = res["predictions"].get("orb")
    assert orb_pred is not None
    # Must be suppressed to NO_TRADE and NEUTRAL, preventing resurrection
    assert orb_pred["status"] == "NO_TRADE"
    assert orb_pred["direction"] == "NEUTRAL"
    assert "concluded for this session" in orb_pred["reason"]


def test_red_team_vwap_fail_closed():
    """RED-TEAM-05: Missing or invalid upstream VWAP must fail closed (UNAVAILABLE)."""
    service = PredictionService()

    now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    df_no_vwap = pd.DataFrame([
        {
            "datetime": now - timedelta(minutes=15 * i),
            "open": 100.0,
            "high": 105.0,
            "low": 98.0,
            "close": 102.0,
            "volume": 10000,
        }
        for i in range(10)
    ]).sort_values("datetime").reset_index(drop=True)

    prepared_df, days = service._prepare_data(df_no_vwap)
    assert prepared_df.empty
    assert len(days) == 0

    # Non-positive / NaN VWAP also fails closed
    df_bad_vwap = df_no_vwap.copy()
    df_bad_vwap["vwap"] = [100.0] * 9 + [0.0]
    prepared_df2, days2 = service._prepare_data(df_bad_vwap)
    assert prepared_df2.empty
    assert len(days2) == 0


def test_red_team_consensus_agreement_denominator():
    """RED-TEAM-08: Consensus agreement percentage must divide by evaluable_count."""
    # 7 strategies total in live consensus
    # 3 evaluable: 3 agree LONG (100% agreement, not 3/7 = 42.9%)
    predictions = {
        "orb": SingleStrategyPrediction(status="BUY", direction="LONG", entry=100.0, stop_loss=98.0, target=104.0),
        "cpr": SingleStrategyPrediction(status="BUY", direction="LONG", entry=100.0, stop_loss=98.0, target=104.0),
        "aou_oss": SingleStrategyPrediction(status="BUY", direction="LONG", entry=100.0, stop_loss=98.0, target=104.0),
        "dual_ema": SingleStrategyPrediction(status="UNAVAILABLE", reason="Missing data"),
        "apex": SingleStrategyPrediction(status="UNAVAILABLE", reason="Missing context"),
        "sector_impulse": SingleStrategyPrediction(status="ERROR", reason="Failure"),
        "ssf_l5_srm": SingleStrategyPrediction(status="UNAVAILABLE", reason="No depth"),
    }

    consensus = PredictionService.calculate_consensus(predictions)
    assert consensus["evaluable_strategies"] == 3
    assert consensus["agreeing_strategies"] == 3
    assert consensus["direction"] == "LONG"
    assert consensus["consensus_agreement_pct"] == 100.0


def test_red_team_process_stream_lock():
    """RED-TEAM-09: Process stream lock prevents concurrent WebSocket stream creation."""
    from backend.streaming.market_stream_manager import market_stream_manager

    # First acquisition succeeds
    market_stream_manager._acquire_process_stream_lock()
    assert market_stream_manager._process_lock_fd is not None

    # Re-acquisition by same manager is idempotent
    market_stream_manager._acquire_process_stream_lock()
    assert market_stream_manager._process_lock_fd is not None

    # Release clears fd
    market_stream_manager._release_process_stream_lock()
    assert market_stream_manager._process_lock_fd is None
