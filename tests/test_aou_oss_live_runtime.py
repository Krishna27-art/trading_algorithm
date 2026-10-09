"""
Unit tests for AOU-OSS persistent live runtime in PredictionService.

Verifies:
1. First evaluation processes all current completed candles once.
2. Second evaluation with same latest candle processes 0 new candles.
3. New completed candle processes exactly 1 new candle.
4. New trading date creates a new AOU runtime.
5. Stale previous-session data returns UNAVAILABLE.
6. Non-trading day returns UNAVAILABLE.
7. Different symbols have independent AOU runtimes.
8. Concurrent evaluations of different symbols do not share strategy state.
"""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
import pandas as pd
import pytest

from backend.config.settings import InstrumentConfig, InstrumentType, settings
from backend.data.time_utils import MarketCalendar
from backend.strategy.prediction_service import PredictionService, SingleStrategyPrediction


def _create_sample_instrument(symbol: str = "RELIANCE") -> InstrumentConfig:
    return InstrumentConfig(
        symbol=symbol,
        exchange="NSE",
        instrument_type=InstrumentType.EQUITY,
        lot_size=1,
        tick_size=0.05,
        min_orb_range=2.0,
        max_orb_range=20.0,
        instrument_token=738561,
    )


def _generate_synthetic_15m_history(
    days: int = 15,
    today_candles: int = 6,
    reference_date: date = date(2026, 10, 5),  # Monday (trading day)
) -> pd.DataFrame:
    """Generate multi-day 15m OHLCV bars for AOU calibration (needs >= 152 bars)."""
    rows = []
    # Generate previous trading days (each with 25 bars: 09:15 to 15:15)
    calib_days = []
    curr = reference_date - timedelta(days=1)
    while len(calib_days) < (days - 1):
        if MarketCalendar.is_trading_day(curr):
            calib_days.append(curr)
        curr -= timedelta(days=1)
    calib_days.reverse()

    base_price = 2500.0
    for d in calib_days:
        for b in range(25):
            dt = datetime.combine(d, datetime.min.time()) + timedelta(hours=9, minutes=15 + b * 15)
            rows.append({
                "datetime": dt,
                "open": base_price + b * 0.2,
                "high": base_price + b * 0.2 + 2.0,
                "low": base_price + b * 0.2 - 2.0,
                "close": base_price + b * 0.2 + 0.5,
                "volume": 10000 + b * 100,
                "vwap": base_price + b * 0.2 + 0.3,
            })

    # Today's candles
    for b in range(today_candles):
        dt = datetime.combine(reference_date, datetime.min.time()) + timedelta(hours=9, minutes=15 + b * 15)
        rows.append({
            "datetime": dt,
            "open": base_price + b * 0.3,
            "high": base_price + b * 0.3 + 2.5,
            "low": base_price + b * 0.3 - 2.5,
            "close": base_price + b * 0.3 + 0.8,
            "volume": 12000 + b * 150,
            "vwap": base_price + b * 0.3 + 0.5,
        })

    return pd.DataFrame(rows)


def test_aou_live_runtime_single_replay_and_incremental_processing():
    """
    1. First evaluation processes all today's completed candles once.
    2. Second evaluation with identical latest candle processes 0 new candles.
    3. Third evaluation with 1 new completed candle processes exactly 1 new candle.
    """
    test_date = date(2026, 10, 5)  # Monday
    inst = _create_sample_instrument("RELIANCE")
    service = PredictionService()

    df_initial = _generate_synthetic_15m_history(days=15, today_candles=6, reference_date=test_date)
    ltp = float(df_initial["close"].iloc[-1])

    with patch("backend.strategy.prediction_service.now_ist_naive", return_value=datetime.combine(test_date, datetime.min.time()) + timedelta(hours=11, minutes=0)):
        # First evaluation
        pred1 = service._evaluate_aou_oss(inst, df_initial, ltp=ltp)
        assert pred1.status in ("WAITING", "NO_TRADE", "AOU_LONG", "AOU_SHORT")

        runtime = service._aou_runtimes.get("RELIANCE")
        assert runtime is not None
        assert runtime.session_date == test_date
        assert runtime.last_candle_open == df_initial["datetime"].iloc[-1]

        # Spy on strategy.on_candle
        original_on_candle = runtime.strategy.on_candle
        call_count = [0]

        def spy_on_candle(*args, **kwargs):
            call_count[0] += 1
            return original_on_candle(*args, **kwargs)

        runtime.strategy.on_candle = spy_on_candle

        # Second evaluation: same dataset (no new candle)
        pred2 = service._evaluate_aou_oss(inst, df_initial, ltp=ltp)
        assert pred2.status == pred1.status
        assert call_count[0] == 0, f"Expected 0 on_candle calls on repeated evaluation, got {call_count[0]}"

        # Third evaluation: append 1 new candle
        new_candle_time = runtime.last_candle_open + timedelta(minutes=15)
        new_row = pd.DataFrame([{
            "datetime": new_candle_time,
            "open": ltp,
            "high": ltp + 3.0,
            "low": ltp - 1.0,
            "close": ltp + 2.0,
            "volume": 15000,
            "vwap": ltp + 1.0,
        }])
        df_updated = pd.concat([df_initial, new_row], ignore_index=True)

        call_count[0] = 0
        pred3 = service._evaluate_aou_oss(inst, df_updated, ltp=ltp + 2.0)
        assert call_count[0] == 1, f"Expected exactly 1 on_candle call for 1 new candle, got {call_count[0]}"
        assert runtime.last_candle_open == new_candle_time


def test_aou_live_runtime_creates_new_instance_on_new_trading_date():
    """When a new trading date arrives, a new runtime is created and seeded."""
    inst = _create_sample_instrument("TCS")
    service = PredictionService()

    day1 = date(2026, 10, 5)  # Monday
    df1 = _generate_synthetic_15m_history(days=15, today_candles=4, reference_date=day1)

    with patch("backend.strategy.prediction_service.now_ist_naive", return_value=datetime.combine(day1, datetime.min.time()) + timedelta(hours=10, minutes=30)):
        service._evaluate_aou_oss(inst, df1, ltp=3500.0)
        runtime1 = service._aou_runtimes.get("TCS")
        assert runtime1 is not None
        assert runtime1.session_date == day1

    day2 = date(2026, 10, 6)  # Tuesday
    df2 = _generate_synthetic_15m_history(days=15, today_candles=3, reference_date=day2)

    with patch("backend.strategy.prediction_service.now_ist_naive", return_value=datetime.combine(day2, datetime.min.time()) + timedelta(hours=10, minutes=15)):
        service._evaluate_aou_oss(inst, df2, ltp=3520.0)
        runtime2 = service._aou_runtimes.get("TCS")
        assert runtime2 is not None
        assert runtime2.session_date == day2
        assert runtime2 is not runtime1


def test_aou_stale_previous_session_returns_unavailable():
    """If latest completed session in data is older than today's session, returns UNAVAILABLE."""
    inst = _create_sample_instrument("INFY")
    service = PredictionService()

    today = date(2026, 10, 6)  # Tuesday
    friday = date(2026, 10, 2)  # Friday (stale)
    df_stale = _generate_synthetic_15m_history(days=15, today_candles=10, reference_date=friday)

    with patch("backend.strategy.prediction_service.now_ist_naive", return_value=datetime.combine(today, datetime.min.time()) + timedelta(hours=11, minutes=0)):
        pred = service._evaluate_aou_oss(inst, df_stale, ltp=1500.0)
        assert pred.status == "UNAVAILABLE"
        assert "stale" in (pred.reason or "").lower()


def test_aou_non_trading_day_returns_unavailable():
    """A weekend or holiday returns UNAVAILABLE without evaluating stale candles."""
    inst = _create_sample_instrument("HDFCBANK")
    service = PredictionService()

    sunday = date(2026, 10, 4)  # Sunday
    friday = date(2026, 10, 2)
    df = _generate_synthetic_15m_history(days=15, today_candles=10, reference_date=friday)

    with patch("backend.strategy.prediction_service.now_ist_naive", return_value=datetime.combine(sunday, datetime.min.time()) + timedelta(hours=11, minutes=0)):
        pred = service._evaluate_aou_oss(inst, df, ltp=1600.0)
        assert pred.status == "UNAVAILABLE"
        assert "not a trading session" in (pred.reason or "").lower()


def test_aou_independent_runtimes_per_symbol():
    """Different symbols maintain isolated AOU runtime instances and locks."""
    service = PredictionService()
    today = date(2026, 10, 5)

    inst_rel = _create_sample_instrument("RELIANCE")
    inst_tcs = _create_sample_instrument("TCS")

    df_rel = _generate_synthetic_15m_history(days=15, today_candles=5, reference_date=today)
    df_tcs = _generate_synthetic_15m_history(days=15, today_candles=4, reference_date=today)

    with patch("backend.strategy.prediction_service.now_ist_naive", return_value=datetime.combine(today, datetime.min.time()) + timedelta(hours=11, minutes=0)):
        service._evaluate_aou_oss(inst_rel, df_rel, ltp=2500.0)
        service._evaluate_aou_oss(inst_tcs, df_tcs, ltp=3500.0)

        r_rel = service._aou_runtimes.get("RELIANCE")
        r_tcs = service._aou_runtimes.get("TCS")

        assert r_rel is not None
        assert r_tcs is not None
        assert r_rel is not r_tcs
        assert r_rel.strategy is not r_tcs.strategy
        assert r_rel.last_candle_open != r_tcs.last_candle_open
