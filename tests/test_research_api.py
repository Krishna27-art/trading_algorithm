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
    """Verifies deterministic consensus calculations across dynamic strategy counts (including 6 strategies)."""
    # 1. Unanimous Long (6/6)
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
    assert c_long["agreeing_strategies"] == 6
    assert c_long["total_strategies"] == 6
    assert c_long["label"] == "STRONG LONG (6/6)"

    # 2. Strong Short 4/6
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
    assert c_short["agreeing_strategies"] == 4
    assert c_short["total_strategies"] == 6
    assert c_short["label"] == "STRONG SHORT (4/6)"

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
    assert "DIVERGENT (2L / 2S)" in c_div["label"]

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
    assert c_neut["total_strategies"] == 6
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


def test_research_backtest_endpoints():
    """Tests GET and POST /api/research/backtest return all 6 strategies and comparison table."""
    # Test GET function
    data = get_research_backtest(days=20, symbol="NIFTY")
    assert data["days"] == 20
    assert data["symbol"] == "NIFTY"
    assert "strategies" in data
    for expected_strat in ["orb", "cpr", "dual_ema", "apex", "sector_impulse", "ssf_l5_srm"]:
        assert expected_strat in data["strategies"]

    required_metrics = [
        "total_trades", "long_trades", "short_trades", "winning_trades", "losing_trades",
        "win_rate_pct", "gross_pnl", "total_transaction_costs", "net_pnl", "profit_factor",
        "sharpe_ratio", "cagr_pct", "max_drawdown_pct", "expectancy_rupees",
        "long_win_rate", "short_win_rate", "long_net_pnl", "short_net_pnl", "yearly_returns",
    ]
    for strat_key in ["orb", "cpr", "dual_ema", "apex", "sector_impulse", "ssf_l5_srm"]:
        strat_report = data["strategies"][strat_key]
        for m in required_metrics:
            assert m in strat_report, f"Missing metric {m} in {strat_key}"

    assert "comparison" in data
    # Now has 6 strategies: ORB, CPR, Dual-EMA, APEX, Sector Impulse, SSF-L5-SRM
    assert len(data["comparison"]) == 6
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
    assert len(data_post["strategies"]) == 6


def test_prediction_service_all_six_strategies():
    """Tests PredictionService.evaluate_symbol produces predictions for all 6 strategies."""
    from data.historical_loader import HistoricalDataLoader

    df = HistoricalDataLoader.generate_synthetic_nifty_data(days=3, seed=42)
    preds, consensus = prediction_service.evaluate_symbol(symbol="NIFTY", df_15m=df, current_ltp=24000.0)

    expected_keys = ["orb", "cpr", "dual_ema", "apex", "sector_impulse", "ssf_l5_srm"]
    assert sorted(list(preds.keys())) == sorted(expected_keys)
    assert consensus["total_strategies"] == 6

    for k in expected_keys:
        p = preds[k]
        assert hasattr(p, "status")
        assert hasattr(p, "direction")
        assert hasattr(p, "entry")
        assert hasattr(p, "stop_loss")
        assert hasattr(p, "target")
        assert hasattr(p, "reason")
