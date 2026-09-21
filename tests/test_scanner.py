"""
Unit Tests for NIFTY 50 Universe Scanner and Explainable Stock Ranker.
"""

from pathlib import Path
import pytest

from config.universe import (
    NIFTY_50_CONSTITUENTS,
    create_instrument_config_for_equity,
    resolve_universe_tokens,
)
from config.settings import InstrumentType
from scanner.stock_ranker import NiftyUniverseScanner, StockRankingMetrics


def test_universe_constituent_count_and_resolution():
    assert len(NIFTY_50_CONSTITUENTS) == 50
    assert "RELIANCE" in NIFTY_50_CONSTITUENTS
    assert "TCS" in NIFTY_50_CONSTITUENTS
    assert "HDFCBANK" in NIFTY_50_CONSTITUENTS

    tokens = resolve_universe_tokens()
    assert len(tokens) == 50
    for sym in NIFTY_50_CONSTITUENTS:
        assert sym in tokens
        assert isinstance(tokens[sym], int)
        assert tokens[sym] > 0


def test_explainable_score_computation():
    scanner = NiftyUniverseScanner()

    # Case 1: High momentum breakout long
    # RVOL=2.5 (>2.0 max 30), Gap=3.0% (>2.5% max 25), ATR%=3.0% (>2.5% max 25), VWAP_dist=+2.0% (>1.5% max 20)
    rvol_sc, gap_sc, vol_sc, vwap_sc, total, bias = scanner.calculate_explainable_score(
        gap_pct=3.0,
        rvol=2.5,
        atr_pct=3.0,
        vwap_dist_pct=2.0,
    )
    assert rvol_sc == 30.0
    assert gap_sc == 25.0
    assert vol_sc == 25.0
    assert vwap_sc == 20.0
    assert total == 100.0
    assert bias == "LONG"

    # Case 2: Moderate short candidate
    rvol_sc2, gap_sc2, vol_sc2, vwap_sc2, total2, bias2 = scanner.calculate_explainable_score(
        gap_pct=-1.25,
        rvol=1.0,
        atr_pct=1.25,
        vwap_dist_pct=-0.75,
    )
    assert 14.0 <= rvol_sc2 <= 16.0
    assert 12.0 <= gap_sc2 <= 13.0
    assert 12.0 <= vol_sc2 <= 13.0
    assert 9.0 <= vwap_sc2 <= 11.0
    assert total2 < 60.0
    assert bias2 == "SHORT"


def test_scanner_synthetic_execution_and_ranking():
    scanner = NiftyUniverseScanner()
    top_5, data_source = scanner.scan_universe(kite_client=None, top_n=5)

    assert data_source == "SYNTHETIC"
    assert len(top_5) == 5

    # Check that ranks are 1 to 5 and sorted descending
    for i, candidate in enumerate(top_5, start=1):
        assert candidate.rank == i
        assert isinstance(candidate, StockRankingMetrics)
        assert candidate.symbol in NIFTY_50_CONSTITUENTS
        assert candidate.ltp > 0
        assert candidate.rvol > 0
        assert candidate.atr_14 > 0
        assert candidate.total_score >= 0.0

    # Ensure strictly sorted by score
    scores = [c.total_score for c in top_5]
    assert scores == sorted(scores, reverse=True)


def test_scanner_instrument_config_factory():
    scanner = NiftyUniverseScanner()
    top_picks, _ = scanner.scan_universe(kite_client=None, top_n=3)
    configs = scanner.get_top_instrument_configs(top_picks)

    assert len(configs) == 3
    for cfg in configs:
        assert cfg.exchange == "NSE"
        assert cfg.instrument_type == InstrumentType.EQUITY
        assert cfg.lot_size == 1
        assert cfg.tick_size == 0.05
        assert cfg.min_orb_range > 0
        assert cfg.max_orb_range > cfg.min_orb_range
        assert cfg.max_risk_cap > 0


def test_create_instrument_config_for_equity():
    cfg = create_instrument_config_for_equity(
        symbol="RELIANCE",
        token=738561,
        current_price=3000.0,
        atr_14=60.0,
    )
    assert cfg.symbol == "RELIANCE"
    assert cfg.instrument_token == 738561
    assert cfg.instrument_type == InstrumentType.EQUITY
    # Scaled from price 3000: min_orb = 5.1, max_orb = 18.0, max_risk = 12.0
    assert cfg.min_orb_range == 5.1
    assert cfg.max_orb_range == 18.0
    assert cfg.max_risk_cap == 12.0


def test_fastapi_scanner_and_multi_symbol_endpoints():
    from backend.main import get_strategy_telemetry, get_universe_scan, trigger_backtest

    scan_res = get_universe_scan(top_n=5)
    assert scan_res["status"] == "success"
    assert scan_res["count"] == 5
    assert len(scan_res["candidates"]) == 5
    assert scan_res["candidates"][0]["rank"] == 1
    assert "total_score" in scan_res["candidates"][0]

    bt_res = trigger_backtest(days=10, symbol="RELIANCE")
    assert bt_res["success"] is True
    assert bt_res["report"]["symbol"] == "RELIANCE"

    tel_res = get_strategy_telemetry(symbol="TCS")
    assert tel_res["symbol"] == "TCS"
    assert len(tel_res["chart_candles"]) > 0


def test_scanner_fails_closed_without_kite():
    scanner = NiftyUniverseScanner()
    with pytest.raises(RuntimeError, match="not authenticated"):
        scanner.scan_universe(kite_client=None, top_n=5, allow_synthetic=False)



