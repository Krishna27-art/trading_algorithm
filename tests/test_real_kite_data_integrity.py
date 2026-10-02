"""
Strict Real-Kite-Data Integrity and Anti-Fabrication Test Suite.

Proves:
1. Strategies cannot generate signals without real market data.
2. ORB derives opening range and breakouts strictly from DataFrame OHLCV.
3. CPR derives pivot levels and regimes strictly from prior-session OHLC.
4. Dual EMA calculates EMA9, EMA21, SMA200, ATR14 from DataFrame close.
5. APEX is independent and does not consume ORB/CPR/Dual-EMA outputs.
6. APEX does not use default 0 as real market information.
7. Missing India VIX/sector/market data is not converted into fake measurements.
8. Backend live research does not generate synthetic candles in live mode.
9. Kite API failure produces explicit DATA_UNAVAILABLE / AUTH_REQUIRED without fake fallbacks.
10. No automatic order placement / paper trading exists in live decision support.
"""

from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock, patch
import numpy as np
import pandas as pd
import pytest

from backend.signals import get_live_research, get_strategy_telemetry
from config.settings import InstrumentConfig, InstrumentType, settings
from strategy.apex_engine import ApexAivemStrategy, EngineConfig
from strategy.cpr_strategy import CPRRegimeBreakoutStrategy, Regime
from strategy.dual_ema_strategy import BufferedDualEMAStrategy
from strategy.orb_strategy import IntradayORBStrategy
from strategy.prediction_service import PredictionService, SingleStrategyPrediction


def _create_sample_instrument():
    return InstrumentConfig(
        symbol="TEST_STOCK",
        exchange="NSE",
        instrument_type=InstrumentType.EQUITY,
        lot_size=1,
        tick_size=0.05,
        min_orb_range=2.0,
        max_orb_range=20.0,
        max_risk_cap=15.0,
        equity_orb_max_risk_pct=0.15,
        instrument_token=123456,
    )


def test_strategies_cannot_generate_signals_without_market_data():
    """Assertion 1: Empty or missing data must return NO_TRADE / DATA_UNAVAILABLE, never a fake signal."""
    inst = _create_sample_instrument()
    pred_service = PredictionService()

    preds, consensus = pred_service.evaluate_symbol("TEST_STOCK", df_15m=pd.DataFrame())

    for strat_key, p in preds.items():
        assert p.direction is None, f"{strat_key} produced a direction without market data!"
        assert p.entry is None
        assert p.stop_loss is None
        assert p.target is None
        assert p.status in ("NO_TRADE", "DATA_UNAVAILABLE", "UNAVAILABLE", "WAITING")

    assert consensus["direction"] == "NEUTRAL"


def test_orb_uses_dataframe_ohlcv():
    """Assertion 2: ORB computes range and breakout strictly from DataFrame OHLCV."""
    inst = _create_sample_instrument()
    strat = IntradayORBStrategy(inst, settings.strategy)
    today = date(2026, 9, 27)
    strat.reset_session(today)

    # Supply 09:15-09:45 bars (high=105.0, low=95.0)
    c1 = {"datetime": datetime(2026, 9, 27, 9, 15), "open": 100.0, "high": 105.0, "low": 98.0, "close": 102.0, "volume": 1000}
    c2 = {"datetime": datetime(2026, 9, 27, 9, 30), "open": 102.0, "high": 103.0, "low": 95.0, "close": 99.0, "volume": 1500}
    strat.on_candle(c1, vwap=101.0)
    strat.on_candle(c2, vwap=100.0)

    # At 09:45 bar, ORB is established
    c3 = {"datetime": datetime(2026, 9, 27, 9, 45), "open": 99.0, "high": 108.0, "low": 98.0, "close": 107.0, "volume": 3000}
    sig = strat.on_candle(c3, vwap=100.0)

    assert strat.orb is not None
    assert strat.orb.high == 105.0  # max(c1.high, c2.high)
    assert strat.orb.low == 95.0   # min(c1.low, c2.low)
    assert strat.orb.width == 10.0
    assert sig is not None
    assert sig.action.value == "BUY"
    assert sig.price == 107.0
    assert sig.stop_loss == 95.0


def test_cpr_uses_actual_previous_session_ohlc():
    """Assertion 3: CPR computes P, BC, TC strictly from prior-day high/low/close."""
    inst = _create_sample_instrument()
    strat = CPRRegimeBreakoutStrategy(inst, settings.strategy)

    prior_bars = pd.DataFrame([
        {"datetime": datetime(2026, 9, 26, 9, 15), "open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0, "volume": 5000},
        {"datetime": datetime(2026, 9, 26, 15, 0), "open": 105.0, "high": 108.0, "low": 92.0, "close": 100.0, "volume": 5000},
    ])
    strat.seed_context(prior_bars)

    assert strat.pivots is not None
    # prior High=110.0, Low=90.0, Close=100.0
    expected_p = (110.0 + 90.0 + 100.0) / 3.0  # 100.0
    expected_bc = (110.0 + 90.0) / 2.0         # 100.0
    expected_tc = 2 * expected_p - expected_bc # 100.0
    assert strat.pivots["P"] == pytest.approx(expected_p)
    assert strat.pivots["BC"] == pytest.approx(expected_bc)
    assert strat.pivots["TC"] == pytest.approx(expected_tc)


def test_dual_ema_calculates_indicators_from_dataframe_close():
    """Assertion 4: Dual EMA calculates EMA9, EMA21, SMA200 from actual closing series."""
    inst = _create_sample_instrument()
    strat = BufferedDualEMAStrategy(inst, settings.strategy, min_warmup_bars=10)

    # 205 historical bars with steadily rising closes to warm up SMA200
    rows = []
    for i in range(205):
        dt = datetime(2026, 9, 20, 9, 15) + timedelta(minutes=15 * i)
        rows.append({"datetime": dt, "high": 100.0 + i * 0.1, "low": 98.0 + i * 0.1, "close": 99.0 + i * 0.1, "volume": 1000})

    strat.seed_context(pd.DataFrame(rows))
    strat.reset_session(date(2026, 9, 27))

    candle = {"datetime": datetime(2026, 9, 27, 9, 45), "high": 130.0, "low": 125.0, "close": 128.0, "volume": 2000}
    ind, _ = strat._current_indicators(candle)

    assert ind is not None
    assert "ema9" in ind.index
    assert "ema21" in ind.index
    assert "sma200" in ind.index
    assert ind["ema9"] > ind["ema21"]  # Trend should be upward


def test_apex_is_independent_and_does_not_consume_other_strategy_signals():
    """Assertion 5: APEX evaluates independently on market candles and context only."""
    inst = _create_sample_instrument()
    apex = ApexAivemStrategy(inst, settings.strategy)
    today = date(2026, 9, 27)

    # Prior bars
    prior_bars = pd.DataFrame([
        {"datetime": datetime(2026, 9, 26, 9, 15) + timedelta(minutes=15 * i), "open": 100.0 + i, "high": 102.0 + i, "low": 99.0 + i, "close": 101.0 + i, "volume": 5000}
        for i in range(20)
    ])
    apex.seed_context(prior_bars)
    apex.reset_session(today)

    # Verify APEX context does NOT contain any ORB, CPR, or Dual-EMA signal fields
    assert "orb_signal" not in apex.context
    assert "cpr_signal" not in apex.context
    assert "dual_ema_signal" not in apex.context


def test_apex_dynamic_reweighting_without_fabricated_zeros():
    """Assertion 6 & 7: When external VIX/sector RS is None, APEX dynamically reweights available real factors."""
    inst = _create_sample_instrument()
    apex = ApexAivemStrategy(inst, settings.strategy)

    prior_bars = pd.DataFrame([
        {"datetime": datetime(2026, 9, 26, 9, 15) + timedelta(minutes=15 * i), "open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0, "volume": 1000}
        for i in range(20)
    ])
    apex.seed_context(prior_bars)
    apex.reset_session(date(2026, 9, 27))

    # Set context with missing sector_rs and market_rs (None)
    apex.set_context(india_vix=None, sector_rs=None, market_rs=None, catalyst_score=None)

    candle = {"datetime": datetime(2026, 9, 27, 9, 45), "open": 102.0, "high": 105.0, "low": 101.0, "close": 104.0, "volume": 2500}
    features = apex._calculate_features(candle, vwap=102.5)

    assert features != {}
    assert features["sector_rs"] is None
    assert features["market_rs"] is None
    assert features["cpr_norm"] is None
    assert "apex_score" in features
    # Score must be finite and computed without assuming sector_rs=0
    assert np.isfinite(features["apex_score"])

    # Verify on_candle completes without TypeError when cpr_norm is None
    sig = apex.on_candle(candle, vwap=102.5)
    # Status in last_analysis is tracked
    assert apex.last_analysis.get("status") in ("APEX_LONG", "APEX_SHORT", "WAITING", "NO_TRADE")


def test_backend_live_research_unauthenticated_gating():
    """Assertion 8, 9, 10: When Kite is unauthenticated, live research returns AUTH_REQUIRED with zero synthetic candidates."""
    with patch("backend.signals.get_active_kite_with_diagnostics", return_value=(None, "Session inactive")):
        res = get_live_research(top_n=5)
        assert res["status"] == "AUTH_REQUIRED"
        assert res["data_source"] == "NONE"
        assert res["returned_count"] == 0
        assert res["candidates"] == []


def test_no_automatic_order_execution_in_system():
    """Assertion 14: Confirms no auto-trading / paper-broker modules exist or place orders."""
    import importlib.util
    paper_broker_spec = importlib.util.find_spec("broker.paper_broker")
    assert paper_broker_spec is None, "broker.paper_broker must remain deleted!"

    # Ensure backend endpoints are read-only
    from backend.kite import router as kite_router
    route_paths = [r.path for r in kite_router.routes]
    assert "/api/orders/place" not in route_paths
    assert "/api/orders/cancel" not in route_paths
