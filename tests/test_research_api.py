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

from backend.main import get_live_research, get_research_backtest, post_research_backtest
from strategy.prediction_service import (
    CandidatePrediction,
    PredictionService,
    SingleStrategyPrediction,
    prediction_service,
)


def test_prediction_service_consensus_rules():
    """Verifies deterministic consensus calculations."""
    # 1. Unanimous Long
    preds_long = {
        "orb": SingleStrategyPrediction(status="LONG_BREAKOUT", direction="LONG"),
        "cpr": SingleStrategyPrediction(status="BULLISH_EXPANSION", direction="LONG"),
        "dual_ema": SingleStrategyPrediction(status="TRENDING_LONG", direction="LONG"),
    }
    c_long = PredictionService.calculate_consensus(preds_long)
    assert c_long["direction"] == "LONG"
    assert c_long["agreeing_strategies"] == 3
    assert c_long["label"] == "UNANIMOUS LONG"

    # 2. Strong Short 2/3
    preds_short = {
        "orb": SingleStrategyPrediction(status="SHORT_BREAKDOWN", direction="SHORT"),
        "cpr": SingleStrategyPrediction(status="BEARISH_EXPANSION", direction="SHORT"),
        "dual_ema": SingleStrategyPrediction(status="NO_TRADE"),
    }
    c_short = PredictionService.calculate_consensus(preds_short)
    assert c_short["direction"] == "SHORT"
    assert c_short["agreeing_strategies"] == 2
    assert c_short["label"] == "STRONG SHORT 2/3"

    # 3. Divergent
    preds_divergent = {
        "orb": SingleStrategyPrediction(status="LONG_BREAKOUT", direction="LONG"),
        "cpr": SingleStrategyPrediction(status="BEARISH_EXPANSION", direction="SHORT"),
        "dual_ema": SingleStrategyPrediction(status="NO_TRADE"),
    }
    c_div = PredictionService.calculate_consensus(preds_divergent)
    assert c_div["direction"] == "DIVERGENT"
    assert c_div["label"] == "DIVERGENT"

    # 4. Neutral
    preds_neutral = {
        "orb": SingleStrategyPrediction(status="NO_TRADE"),
        "cpr": SingleStrategyPrediction(status="NO_TRADE"),
        "dual_ema": SingleStrategyPrediction(status="BUFFER_ZONE"),
    }
    c_neut = PredictionService.calculate_consensus(preds_neutral)
    assert c_neut["direction"] == "NEUTRAL"
    assert c_neut["agreeing_strategies"] == 0
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
        },
        consensus={"direction": "LONG", "agreeing_strategies": 3, "total_strategies": 3, "label": "UNANIMOUS LONG"},
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
        },
        consensus={"direction": "SHORT", "agreeing_strategies": 3, "total_strategies": 3, "label": "UNANIMOUS SHORT"},
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
        },
        consensus={"direction": "DIVERGENT", "agreeing_strategies": 1, "total_strategies": 3, "label": "DIVERGENT"},
    )

    insights = PredictionService.extract_key_insights([c1, c2, c3])
    assert insights["top_long"]["symbol"] == "SBILIFE"
    assert insights["top_short"]["symbol"] == "INFY"
    assert insights["strongest_consensus"]["symbol"] == "SBILIFE"
    assert len(insights["divergent_signals"]) == 1
    assert insights["divergent_signals"][0]["symbol"] == "TCS"


def test_get_research_live_endpoint():
    """Tests get_live_research returns proper contract in test mode."""
    data = get_live_research(top_n=5, force_refresh=True)
    assert data["status"] == "success"
    assert "data_source" in data
    assert "timestamp" in data
    assert "market_status" in data
    assert data["scanned_count"] == 50
    assert len(data["candidates"]) == 5

    cand = data["candidates"][0]
    assert "rank" in cand
    assert "symbol" in cand
    assert "ltp" in cand
    assert "momentum_score" in cand
    assert "universe_bias" in cand
    assert "predictions" in cand
    assert "orb" in cand["predictions"]
    assert "cpr" in cand["predictions"]
    assert "dual_ema" in cand["predictions"]
    assert "consensus" in cand
    assert "label" in cand["consensus"]

    assert "key_insights" in data
    assert "top_long" in data["key_insights"]
    assert "top_short" in data["key_insights"]
    assert "strongest_consensus" in data["key_insights"]
    assert "divergent_signals" in data["key_insights"]


def test_research_backtest_endpoints():
    """Tests GET and POST /api/research/backtest return all 3 strategies and comparison table."""
    # Test GET function
    data = get_research_backtest(days=20, symbol="NIFTY")
    assert data["days"] == 20
    assert data["symbol"] == "NIFTY"
    assert "strategies" in data
    assert "orb" in data["strategies"]
    assert "cpr" in data["strategies"]
    assert "dual_ema" in data["strategies"]

    required_metrics = [
        "total_trades", "long_trades", "short_trades", "winning_trades", "losing_trades",
        "win_rate_pct", "gross_pnl", "total_transaction_costs", "net_pnl", "profit_factor",
        "sharpe_ratio", "cagr_pct", "max_drawdown_pct", "expectancy_rupees",
        "long_win_rate", "short_win_rate", "long_net_pnl", "short_net_pnl", "yearly_returns",
    ]
    for strat_key in ["orb", "cpr", "dual_ema"]:
        strat_report = data["strategies"][strat_key]
        for m in required_metrics:
            assert m in strat_report, f"Missing metric {m} in {strat_key}"

    assert "comparison" in data
    assert len(data["comparison"]) == 3
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
