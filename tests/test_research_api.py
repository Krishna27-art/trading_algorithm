"""
Unit and integration tests for Unified Research & Multi-Strategy Prediction APIs.
Tests:
- GET /api/research/live
- GET/POST /api/research/backtest
- PredictionService evaluation, consensus rules, and key insights extraction
"""

import os
import pytest
from datetime import datetime

from backend.signals import get_live_research, get_research_backtest, post_research_backtest
from strategy.prediction_service import (
    CandidatePrediction,
    PredictionService,
    SingleStrategyPrediction,
    prediction_service,
)


def test_prediction_service_consensus_rules():
    """Verifies deterministic consensus calculations across dynamic strategy counts (4 live strategies)."""
    # 1. Unanimous Long (4/4 live)
    preds_long = {
        "orb": SingleStrategyPrediction(status="LONG_BREAKOUT", direction="LONG"),
        "cpr": SingleStrategyPrediction(status="BULLISH_EXPANSION", direction="LONG"),
        "dual_ema": SingleStrategyPrediction(status="TRENDING_LONG", direction="LONG"),
        "apex": SingleStrategyPrediction(status="APEX_LONG", direction="LONG"),
        "sector_impulse": SingleStrategyPrediction(status="IMPULSE_LONG", direction="LONG"),
        "ssf_l5_srm": SingleStrategyPrediction(status="SSF_LONG", direction="LONG"),
    }
    c_long = PredictionService.calculate_consensus(preds_long)
    assert c_long["direction"] == "LONG"
    assert c_long["agreeing_strategies"] == 4
    assert c_long["total_strategies"] == 4
    assert c_long["evaluable_strategies"] == 4
    assert c_long["label"] == "STRONG LONG (4/4)"
    assert c_long["excluded_strategies"] == ["sector_impulse", "ssf_l5_srm"]

    # 2. Strong Short 3/4
    preds_short = {
        "orb": SingleStrategyPrediction(status="SHORT_BREAKDOWN", direction="SHORT"),
        "cpr": SingleStrategyPrediction(status="BEARISH_EXPANSION", direction="SHORT"),
        "dual_ema": SingleStrategyPrediction(status="NO_TRADE"),
        "apex": SingleStrategyPrediction(status="APEX_SHORT", direction="SHORT"),
        "sector_impulse": SingleStrategyPrediction(status="IMPULSE_SHORT", direction="SHORT"),
        "ssf_l5_srm": SingleStrategyPrediction(status="NO_TRADE"),
    }
    c_short = PredictionService.calculate_consensus(preds_short)
    assert c_short["direction"] == "SHORT"
    assert c_short["agreeing_strategies"] == 3
    assert c_short["total_strategies"] == 4
    assert c_short["evaluable_strategies"] == 4
    assert c_short["label"] == "STRONG SHORT (3/4)"

    # 3. Divergent with new strategies
    preds_divergent = {
        "orb": SingleStrategyPrediction(status="LONG_BREAKOUT", direction="LONG"),
        "cpr": SingleStrategyPrediction(status="BEARISH_EXPANSION", direction="SHORT"),
        "dual_ema": SingleStrategyPrediction(status="NO_TRADE"),
        "apex": SingleStrategyPrediction(status="NO_TRADE"),
        "sector_impulse": SingleStrategyPrediction(status="IMPULSE_LONG", direction="LONG"),
        "ssf_l5_srm": SingleStrategyPrediction(status="SSF_SHORT", direction="SHORT"),
    }
    c_div = PredictionService.calculate_consensus(preds_divergent)
    assert c_div["direction"] == "DIVERGENT"
    assert "DIVERGENT (1L / 1S)" in c_div["label"]

    # 4. Neutral
    preds_neutral = {
        "orb": SingleStrategyPrediction(status="NO_TRADE"),
        "cpr": SingleStrategyPrediction(status="NO_TRADE"),
        "dual_ema": SingleStrategyPrediction(status="BUFFER_ZONE"),
        "apex": SingleStrategyPrediction(status="NO_TRADE"),
        "sector_impulse": SingleStrategyPrediction(status="NO_TRADE"),
        "ssf_l5_srm": SingleStrategyPrediction(status="NO_TRADE"),
    }
    c_neut = PredictionService.calculate_consensus(preds_neutral)
    assert c_neut["direction"] == "NEUTRAL"
    assert c_neut["agreeing_strategies"] == 0
    assert c_neut["total_strategies"] == 4
    assert c_neut["label"] == "NEUTRAL"


def test_prediction_service_extract_key_insights():
    """Verifies key insights extraction across multiple candidates."""
    c1 = CandidatePrediction(
        rank=1,
        symbol="SBILIFE",
        ltp=1750.0,
        momentum_score=45.0,
        universe_bias="LONG",
        predictions={
            "orb": SingleStrategyPrediction(status="LONG_BREAKOUT", direction="LONG", reason="ORB long"),
            "cpr": SingleStrategyPrediction(status="BULLISH_EXPANSION", direction="LONG", reason="CPR bullish"),
            "dual_ema": SingleStrategyPrediction(status="TRENDING_LONG", direction="LONG", reason="Dual EMA trend"),
            "apex": SingleStrategyPrediction(status="APEX_LONG", direction="LONG", reason="APEX long"),
            "sector_impulse": SingleStrategyPrediction(status="IMPULSE_LONG", direction="LONG", reason="SIT long"),
            "ssf_l5_srm": SingleStrategyPrediction(status="SSF_LONG", direction="LONG", reason="SSF long"),
        },
        consensus={"direction": "LONG", "agreeing_strategies": 6, "total_strategies": 6, "label": "STRONG LONG (6/6)"},
    )
    c2 = CandidatePrediction(
        rank=2,
        symbol="INFY",
        ltp=1035.0,
        momentum_score=38.0,
        universe_bias="SHORT",
        predictions={
            "orb": SingleStrategyPrediction(status="SHORT_BREAKDOWN", direction="SHORT", reason="ORB short"),
            "cpr": SingleStrategyPrediction(status="BEARISH_EXPANSION", direction="SHORT", reason="CPR bearish"),
            "dual_ema": SingleStrategyPrediction(status="TRENDING_SHORT", direction="SHORT", reason="Dual EMA short"),
            "apex": SingleStrategyPrediction(status="APEX_SHORT", direction="SHORT", reason="APEX short"),
            "sector_impulse": SingleStrategyPrediction(status="IMPULSE_SHORT", direction="SHORT", reason="SIT short"),
            "ssf_l5_srm": SingleStrategyPrediction(status="NO_TRADE", reason="SSF neutral"),
        },
        consensus={"direction": "SHORT", "agreeing_strategies": 5, "total_strategies": 6, "label": "STRONG SHORT (5/6)"},
    )
    c3 = CandidatePrediction(
        rank=3,
        symbol="TCS",
        ltp=3500.0,
        momentum_score=20.0,
        universe_bias="NEUTRAL",
        predictions={
            "orb": SingleStrategyPrediction(status="LONG_BREAKOUT", direction="LONG", reason="ORB long"),
            "cpr": SingleStrategyPrediction(status="BEARISH_EXPANSION", direction="SHORT", reason="CPR short"),
            "dual_ema": SingleStrategyPrediction(status="NO_TRADE"),
            "apex": SingleStrategyPrediction(status="NO_TRADE"),
            "sector_impulse": SingleStrategyPrediction(status="NO_TRADE"),
            "ssf_l5_srm": SingleStrategyPrediction(status="NO_TRADE"),
        },
        consensus={"direction": "DIVERGENT", "agreeing_strategies": 1, "total_strategies": 6, "label": "DIVERGENT (1L / 1S)"},
    )

    insights = PredictionService.extract_key_insights([c1, c2, c3])
    assert insights["top_long"]["symbol"] == "SBILIFE"
    assert insights["top_short"]["symbol"] == "INFY"
    assert insights["strongest_consensus"]["symbol"] == "SBILIFE"
    assert len(insights["divergent_signals"]) == 1
    assert insights["divergent_signals"][0]["symbol"] == "TCS"


def test_get_research_live_endpoint():
    """Tests get_live_research returns proper auth requirement when offline."""
    data = get_live_research(top_n=5, force_refresh=True)
    assert data["status"] in ("AUTH_REQUIRED", "success", "DATA_UNAVAILABLE")
    assert "timestamp" in data
    assert "market_status" in data


def test_research_backtest_endpoints(monkeypatch):
    """Tests GET and POST /api/research/backtest return all 6 strategies and comparison table."""
    from data.historical_loader import HistoricalDataLoader

    df_fixture = HistoricalDataLoader.generate_synthetic_nifty_data(days=20, base_price=24000.0)
    monkeypatch.setattr(
        HistoricalDataLoader,
        "load_cached_data_with_validation",
        staticmethod(lambda path: (df_fixture, {})),
    )
    monkeypatch.setattr(
        "pathlib.Path.exists",
        lambda self: True,
    )

    # Test GET function
    data = get_research_backtest(days=20, symbol="NIFTY")
    assert data["days"] == 20
    assert data["symbol"] == "NIFTY"
    assert "strategies" in data
    for expected_strat in ["orb", "cpr", "dual_ema", "apex", "sector_impulse", "ssf_l5_srm", "aou_oss"]:
        assert expected_strat in data["strategies"]

    required_metrics = [
        "total_trades", "long_trades", "short_trades", "winning_trades", "losing_trades",
        "win_rate_pct", "gross_pnl", "total_transaction_costs", "net_pnl", "profit_factor",
        "sharpe_ratio", "cagr_pct", "max_drawdown_pct", "expectancy_rupees",
        "long_win_rate", "short_win_rate", "long_net_pnl", "short_net_pnl", "yearly_returns",
    ]
    for strat_key in ["orb", "cpr", "dual_ema", "apex", "sector_impulse", "ssf_l5_srm", "aou_oss"]:
        strat_report = data["strategies"][strat_key]
        for m in required_metrics:
            assert m in strat_report, f"Missing metric {m} in {strat_key}"

    assert "comparison" in data
    # Now has 7 strategies: ORB, CPR, Dual-EMA, APEX, Sector Impulse, SSF-L5-SRM, AOU-OSS
    assert len(data["comparison"]) == 7
    for comp in data["comparison"]:
        assert "strategy" in comp
        assert "trades" in comp
        assert "win_rate" in comp
        assert "profit_factor" in comp
        assert "sharpe" in comp
        assert "max_drawdown" in comp
        assert "net_pnl" in comp
        assert "state" in comp

    # Test POST function
    data_post = post_research_backtest(days=20, symbol="NIFTY")
    assert data_post["days"] == 20
    assert len(data_post["strategies"]) == 7


def test_prediction_service_all_strategies():
    """Tests PredictionService.evaluate_symbol produces predictions for all 7 strategies."""
    from data.historical_loader import HistoricalDataLoader

    df = HistoricalDataLoader.generate_synthetic_nifty_data(days=15, seed=42)
    preds, consensus = prediction_service.evaluate_symbol(symbol="NIFTY", df_15m=df, current_ltp=24000.0)

    expected_keys = ["orb", "cpr", "dual_ema", "apex", "sector_impulse", "ssf_l5_srm", "aou_oss"]
    assert sorted(list(preds.keys())) == sorted(expected_keys)
    assert consensus["total_strategies"] == 4
    assert consensus["evaluable_strategies"] == 4
    assert consensus["excluded_strategies"] == [
        "aou_oss",
        "sector_impulse",
        "ssf_l5_srm",
    ]

    for k in expected_keys:
        p = preds[k]
        assert hasattr(p, "status")
        assert hasattr(p, "direction")
        assert hasattr(p, "entry")
        assert hasattr(p, "stop_loss")
        assert hasattr(p, "target")
        assert hasattr(p, "reason")


def test_consensus_with_unavailable_and_no_trade_strategies():
    """Verifies live consensus uses only the four live-enabled strategies."""
    preds_2_long_4_notrade = {
        "orb": SingleStrategyPrediction(status="LONG_BREAKOUT", direction="LONG"),
        "cpr": SingleStrategyPrediction(status="BULLISH_EXPANSION", direction="LONG"),
        "dual_ema": SingleStrategyPrediction(status="NO_TRADE"),
        "apex": SingleStrategyPrediction(status="NO_TRADE"),
        "sector_impulse": SingleStrategyPrediction(status="NO_TRADE"),
        "ssf_l5_srm": SingleStrategyPrediction(status="NO_TRADE"),
    }
    c1 = PredictionService.calculate_consensus(preds_2_long_4_notrade)
    assert c1["direction"] == "LONG"
    assert c1["agreeing_strategies"] == 2
    assert c1["total_strategies"] == 4
    assert c1["label"] == "MODERATE LONG (2/4)"

    # All unavailable
    preds_all_unavail = {
        k: SingleStrategyPrediction(status="UNAVAILABLE", reason="feed offline")
        for k in ["orb", "cpr", "dual_ema", "apex", "sector_impulse", "ssf_l5_srm"]
    }
    c_unavail = PredictionService.calculate_consensus(preds_all_unavail)
    assert c_unavail["direction"] == "NEUTRAL"
    assert c_unavail["label"] == "UNAVAILABLE"
    assert c_unavail["evaluable_strategies"] == 0


def test_prediction_service_with_real_book_snapshot_and_peer_context():
    """Verifies evaluate_symbol successfully incorporates BookSnapshot and PeerContext."""
    from data.historical_loader import HistoricalDataLoader
    from strategy.ssf_l5_srm_strategy import BookSnapshot
    from strategy.sector_impulse_strategy import PeerContext

    df_own = HistoricalDataLoader.generate_synthetic_nifty_data(days=5, seed=50, base_price=1000.0)
    df_l = HistoricalDataLoader.generate_synthetic_nifty_data(days=5, seed=51, base_price=2500.0)
    df_m = HistoricalDataLoader.generate_synthetic_nifty_data(days=5, seed=52, base_price=24000.0)
    df_sec = HistoricalDataLoader.generate_synthetic_nifty_data(days=5, seed=53, base_price=5000.0)

    ctx = PeerContext(leader=df_l, market=df_m, sector=df_sec)
    snap = BookSnapshot(
        timestamp=datetime.now(),
        bids=[(999.0 - i * 0.05, 1000, 1) for i in range(5)],
        asks=[(1001.0 + i * 0.05, 1000, 1) for i in range(5)],
        ltp=1000.0,
    )

    preds, consensus = prediction_service.evaluate_symbol(
        symbol="SBIN",
        df_15m=df_own,
        current_ltp=1000.0,
        book_snapshot=snap,
        peer_context=ctx,
    )

    assert preds["sector_impulse"].status in ("IMPULSE_LONG", "IMPULSE_SHORT", "MONITORING", "NO_TRADE")
    assert preds["ssf_l5_srm"].status in ("SSF_LONG", "SSF_SHORT", "WAITING", "NO_TRADE")


def test_consensus_ignores_non_live_strategies():
    """
    SIT and SSF-L5-SRM must never affect live consensus until their live
    signal paths are explicitly enabled.
    """
    predictions = {
        "orb": SingleStrategyPrediction(
            status="LONG_BREAKOUT",
            direction="LONG",
        ),
        "cpr": SingleStrategyPrediction(
            status="LONG_BREAKOUT",
            direction="LONG",
        ),
        "dual_ema": SingleStrategyPrediction(
            status="NO_TRADE",
        ),
        "apex": SingleStrategyPrediction(
            status="NO_TRADE",
        ),
        # These must be ignored even though they contain directions.
        "sector_impulse": SingleStrategyPrediction(
            status="IMPULSE_SHORT",
            direction="SHORT",
        ),
        "ssf_l5_srm": SingleStrategyPrediction(
            status="SSF_SHORT",
            direction="SHORT",
        ),
    }

    consensus = PredictionService.calculate_consensus(predictions)

    assert consensus["direction"] == "LONG"
    assert consensus["agreeing_strategies"] == 2
    assert consensus["total_strategies"] == 4
    assert consensus["evaluable_strategies"] == 4
    assert consensus["label"] == "MODERATE LONG (2/4)"
    assert consensus["excluded_strategies"] == [
        "sector_impulse",
        "ssf_l5_srm",
    ]


def test_latest_completed_15m_candle_boundary():
    from datetime import datetime

    service = PredictionService()

    assert (
        service._latest_completed_15m_start(
            datetime(2026, 10, 1, 9, 20)
        )
        is None
    )

    assert (
        service._latest_completed_15m_start(
            datetime(2026, 10, 1, 9, 30)
        )
        == datetime(2026, 10, 1, 9, 15)
    )

    assert (
        service._latest_completed_15m_start(
            datetime(2026, 10, 1, 10, 7)
        )
        == datetime(2026, 10, 1, 9, 45)
    )

    assert (
        service._latest_completed_15m_start(
            datetime(2026, 10, 1, 14, 7)
        )
        == datetime(2026, 10, 1, 13, 45)
    )


def test_price_breached_signal_is_invalidated():

    prediction = SingleStrategyPrediction(
        status="LONG_BREAKOUT",
        direction="LONG",
        entry=1012.80,
        stop_loss=1004.08,
        target=1025.00,
    )

    result = PredictionService._invalidate_price_breached_signal(
        prediction,
        current_ltp=977.80,
    )

    assert result.direction is None
    assert result.status == "NO_TRADE"


def test_valid_current_signal_is_not_invalidated():

    prediction = SingleStrategyPrediction(
        status="LONG_BREAKOUT",
        direction="LONG",
        entry=1012.80,
        stop_loss=1004.08,
        target=1025.00,
    )

    result = PredictionService._invalidate_price_breached_signal(
        prediction,
        current_ltp=1016.00,
    )

    assert result.direction == "LONG"
    assert result.status == "LONG_BREAKOUT"


def test_consensus_agreement_is_vote_fraction_not_probability():
    predictions = {
        "orb": SingleStrategyPrediction(
            status="LONG_BREAKOUT",
            direction="LONG",
        ),
        "cpr": SingleStrategyPrediction(
            status="BULLISH_EXPANSION",
            direction="LONG",
        ),
        "dual_ema": SingleStrategyPrediction(
            status="NO_TRADE",
        ),
        "apex": SingleStrategyPrediction(
            status="NO_TRADE",
        ),
    }

    consensus = PredictionService.calculate_consensus(predictions)

    assert consensus["agreeing_strategies"] == 2
    assert consensus["total_strategies"] == 4
    assert consensus["consensus_agreement_pct"] == 50.0


def test_consensus_agreement_is_none_when_no_strategies_exist():
    consensus = PredictionService.calculate_consensus({})

    assert consensus["agreeing_strategies"] == 0
    assert consensus["total_strategies"] == 0
    assert consensus["consensus_agreement_pct"] is None


def test_strategy_trades_returns_live_only(tmp_path, monkeypatch):
    from database.db import DatabaseManager
    from database.models import OrderDirection, TradeRecord
    from config import settings as config_settings
    from backend import signals

    db_file = tmp_path / "trading_system.db"
    db = DatabaseManager(db_file)

    def record(trade_id, is_paper):
        db.record_trade_entry(
            TradeRecord(
                trade_id=trade_id,
                symbol="SBIN",
                direction=OrderDirection.BUY,
                entry_time=datetime(2026, 10, 1, 10, 0),
                entry_price=1000.0,
                quantity=1,
                initial_stop=990.0,
                initial_target=1020.0,
                is_paper=is_paper,
            )
        )

    record("BT_TEST", True)
    record("LIVE_TEST", False)

    from config.settings import settings as app_settings
    monkeypatch.setattr(app_settings, "db_path", db_file)

    result = signals.get_strategy_trades()

    assert result["count"] == 1
    assert result["trades"][0]["trade_id"] == "LIVE_TEST"




