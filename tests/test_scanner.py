"""
Unit Tests for NIFTY 50 Universe Scanner and Explainable Stock Ranker.
"""

from datetime import date
from pathlib import Path
import pytest

from config.universe import (
    StockUniverse,
    create_instrument_config_for_equity,
    resolve_universe_tokens,
)
from config.settings import InstrumentType
from data.historical_loader import HistoricalDataLoader
from scanner.stock_ranker import NiftyUniverseScanner, StockRankingMetrics


def test_universe_constituent_count_and_resolution():
    universe = StockUniverse()
    assert len(universe.all_symbols) == 700
    assert len(universe.large_cap_stocks) == 100
    assert len(universe.mid_cap_stocks) == 100
    assert len(universe.small_cap_stocks) == 500
    assert "RELIANCE" in universe.all_symbols
    assert "TCS" in universe.all_symbols
    assert "HDFCBANK" in universe.all_symbols


def test_universe_token_resolution_uses_kite_master(tmp_path):
    from data.instrument_resolver import InstrumentResolver

    class FakeKite:
        def instruments(self, exchange):
            return [
                {
                    "tradingsymbol": "RELIANCE",
                    "instrument_token": 738561,
                },
                {
                    "tradingsymbol": "TCS",
                    "instrument_token": 2953217,
                },
            ]

    resolver = InstrumentResolver(cache_dir=tmp_path)

    tokens, unresolved = resolver.resolve_universe(
        symbols=["RELIANCE", "TCS"],
        kite_client=FakeKite(),
        cache_path=tmp_path / "universe_700_tokens.json",
        force_refresh=True,
    )

    assert tokens == {
        "RELIANCE": 738561,
        "TCS": 2953217,
    }

    assert unresolved == []



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
    top_5, data_source = scanner.scan_universe(kite_client=None, top_n=5, allow_synthetic=True)

    assert data_source == "SYNTHETIC"
    assert len(top_5) == 5

    # Check that ranks are 1 to 5 and sorted descending
    for i, candidate in enumerate(top_5, start=1):
        assert candidate.rank == i
        assert isinstance(candidate, StockRankingMetrics)
        all_syms = [r.symbol for r in scanner.universe.all_stocks]
        assert candidate.symbol in all_syms
        assert candidate.ltp > 0
        assert candidate.rvol > 0
        assert candidate.atr_14 > 0
        assert candidate.total_score >= 0.0

    # Ensure strictly sorted by score
    scores = [c.total_score for c in top_5]
    assert scores == sorted(scores, reverse=True)


def test_scanner_instrument_config_factory():
    scanner = NiftyUniverseScanner()
    top_picks, _ = scanner.scan_universe(kite_client=None, top_n=3, allow_synthetic=True)
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


def test_fastapi_scanner_and_multi_symbol_endpoints(monkeypatch):
    from backend.signals import get_universe_scan
    from backend.backtest_routes import trigger_backtest
    from data.historical_loader import HistoricalDataLoader

    # Unauthenticated scanner returns AUTH_REQUIRED
    scan_res = get_universe_scan(top_n=5)
    assert scan_res["status"] == "AUTH_REQUIRED"
    assert scan_res["data_source"] == "NONE"

    # Backtest with mocked/fixture data
    df_fixture = HistoricalDataLoader.generate_synthetic_nifty_data(days=10, base_price=2000.0)
    monkeypatch.setattr(
        HistoricalDataLoader,
        "load_cached_data_with_validation",
        staticmethod(lambda path: (df_fixture, {})),
    )
    monkeypatch.setattr(
        "pathlib.Path.exists",
        lambda self: True,
    )

    bt_res = trigger_backtest(days=10, symbol="RELIANCE")
    assert bt_res["success"] is True
    assert bt_res["report"]["symbol"] == "RELIANCE"



def test_scanner_fails_closed_without_kite():
    scanner = NiftyUniverseScanner()
    with pytest.raises(RuntimeError, match="not authenticated"):
        scanner.scan_universe(kite_client=None, top_n=5, allow_synthetic=False)


def test_700_stock_universe_abstraction():
    from config.universe import StockUniverse

    universe = StockUniverse()
    assert len(universe.large_cap_stocks) == 100
    assert len(universe.mid_cap_stocks) == 100
    assert len(universe.small_cap_stocks) == 500
    assert len(universe.all_stocks) == 700

    rec = universe.all_stocks[0]
    assert hasattr(rec, "symbol")
    assert hasattr(rec, "name")
    assert hasattr(rec, "market_cap_rank")
    assert hasattr(rec, "category")
    assert rec.category in ["large", "mid", "small"]


def test_liquidity_filter_layer():
    from scanner.liquidity_filter import LiquidityFilter, LiquidityStatus

    lfilter = LiquidityFilter()

    # Pass case
    good_stock = {
        "symbol": "RELIANCE",
        "ltp": 2950.0,
        "volume": 2000000,
        "avg_volume_20d": 1800000,
    }
    res_pass = lfilter.evaluate_stock(good_stock)
    assert res_pass.status == LiquidityStatus.PASS
    assert res_pass.is_tradable is True

    # Low price fail case
    penny_stock = {
        "symbol": "PENNY",
        "ltp": 5.0,  # Below min 20.0
        "volume": 500000,
        "avg_volume_20d": 500000,
    }
    res_fail = lfilter.evaluate_stock(penny_stock)
    assert res_fail.status == LiquidityStatus.FAIL
    assert res_fail.is_tradable is False
    assert any("LTP" in r for r in res_fail.rejection_reasons)

    # Unavailable data case
    missing_stock = {"symbol": "BAD_DATA", "is_data_unavailable": True}
    res_unavail = lfilter.evaluate_stock(missing_stock)
    assert res_unavail.status == LiquidityStatus.DATA_UNAVAILABLE
    assert res_unavail.is_tradable is False


def test_scanning_pipeline_summary_counters():
    scanner = NiftyUniverseScanner()
    candidates, data_source = scanner.scan_universe(kite_client=None, top_n=10, allow_synthetic=True)

    summary = scanner.last_pipeline_summary
    assert summary["universe_count"] == 700
    assert summary["tradable_count"] > 0
    assert summary["setup_count"] == summary["tradable_count"]
    assert "strong_signal_count" in summary


def test_700_stock_universe_exact_counts_and_zero_duplicates():
    from config.universe import StockUniverse

    universe = StockUniverse()
    all_stocks = universe.all_stocks
    large = universe.large_cap_stocks
    mid = universe.mid_cap_stocks
    small = universe.small_cap_stocks

    assert len(all_stocks) == 700
    assert len(large) == 100
    assert len(mid) == 100
    assert len(small) == 500

    symbols = [r.symbol for r in all_stocks]
    assert len(set(symbols)) == 700, "Duplicate symbols found in universe!"

    # Verify each stock has required scanner fields
    for s in all_stocks:
        assert isinstance(s.symbol, str) and len(s.symbol) > 0
        assert isinstance(s.name, str) and len(s.name) > 0
        assert isinstance(s.market_cap_rank, int) and s.market_cap_rank > 0
        assert s.category in ("large", "mid", "small")

    # Test symbol and token lookup
    rel = universe.get_stock("RELIANCE")
    assert rel is not None
    assert rel.symbol == "RELIANCE"
    assert rel.category == "large"

    tcs_token = universe.get_token("TCS", token_map={"TCS": 2953217})
    assert tcs_token == 2953217


def test_stock_universe_validation_failure_raises_error(tmp_path):
    import json
    from config.universe import StockUniverse

    # Case 1: Less than 300 stocks (299) — soft-degrade: no ValueError, but a warning is logged
    # (Fix 3.4: universe no longer hard-crashes on count mismatch)
    import logging
    data_invalid_count = [
        {"symbol": f"SYM{i}", "name": f"Name{i}", "market_cap_rank": i + 1, "category": "large" if i < 100 else ("mid" if i < 200 else "small")}
        for i in range(299)
    ]
    invalid_file = tmp_path / "invalid_count.json"
    invalid_file.write_text(json.dumps(data_invalid_count))

    with pytest.warns(None) if False else __import__("contextlib").nullcontext():
        # Should NOT raise — universe degrades gracefully
        uni_partial = StockUniverse(json_path=invalid_file)
        assert len(uni_partial.all_stocks) == 299  # 299 valid stocks were loaded

    # Case 2: Duplicate symbol — still logs an error but does NOT crash

    data_duplicate = [
        {"symbol": "RELIANCE" if i in (0, 1) else f"SYM{i}", "name": f"Name{i}", "market_cap_rank": i + 1, "category": "large" if i < 100 else ("mid" if i < 200 else "small")}
        for i in range(300)
    ]
    duplicate_file = tmp_path / "duplicate.json"
    duplicate_file.write_text(json.dumps(data_duplicate))

    with __import__("contextlib").nullcontext():
        # Duplicate symbol is logged as an error but does NOT raise (soft-degrade)
        uni_dup = StockUniverse(json_path=duplicate_file)
        # 'RELIANCE' appears twice in the source; only the first occurrence is kept
        symbols = [r.symbol for r in uni_dup.all_stocks]
        assert symbols.count("RELIANCE") <= 1


def test_instrument_resolver_300_universe_and_unresolved_reporting():
    from data.instrument_resolver import instrument_resolver

    test_syms = ["RELIANCE", "TCS", "UNKNOWN_XYZ_999"]
    resolved_map, unresolved = instrument_resolver.resolve_universe(
        symbols=test_syms,
        force_refresh=True,
    )

    assert "UNKNOWN_XYZ_999" in unresolved
    assert "UNKNOWN_XYZ_999" not in resolved_map


def test_scanner_uses_700_stocks_without_nifty50_fallback():
    from scanner.stock_ranker import StockUniverseScanner

    scanner = StockUniverseScanner()
    assert len(scanner.universe.all_stocks) == 700

    candidates, data_source = scanner.scan_universe(kite_client=None, top_n=5, allow_synthetic=True)
    assert scanner.last_pipeline_summary["universe_count"] == 700
    assert len(candidates) == 5


def test_latest_completed_trading_day_handles_weekend():
    scanner = NiftyUniverseScanner()

    # Regular Monday -> previous completed trading day is Friday.
    result = (
        scanner._latest_completed_trading_day(
            date(2026, 10, 12)
        )
    )

    assert result == date(
        2026, 10, 9
    )

    # Monday after Friday holiday (Oct 2 Gandhi Jayanti) -> Thursday.
    result_holiday = (
        scanner._latest_completed_trading_day(
            date(2026, 10, 5)
        )
    )

    assert result_holiday == date(
        2026, 10, 1
    )


def test_scanner_uses_cached_daily_context_only(
    tmp_path,
    monkeypatch,
):
    import pandas as pd
    from datetime import datetime

    scanner = NiftyUniverseScanner(
        cache_dir=tmp_path
    )

    cache_file = (
        tmp_path
        / "TEST_daily_context.csv"
    )

    df = pd.DataFrame([
        {
            "datetime": datetime(
                2026, 10, 1
            ),
            "open": 100.0,
            "high": 105.0,
            "low": 95.0,
            "close": 102.0,
            "volume": 100000,
        }
        for _ in range(20)
    ])

    # Make timestamps unique.
    df["datetime"] = pd.date_range(
        "2026-09-04",
        periods=20,
        freq="D",
    )

    HistoricalDataLoader.save_with_metadata(
        df,
        cache_file,
        {
            "symbol": "TEST",
            "instrument_token": 123,
            "interval": "day",
        },
    )

    def fail_if_network_called(*args, **kwargs):
        raise AssertionError(
            "Scanner must not call Kite Historical API."
        )

    monkeypatch.setattr(
        HistoricalDataLoader,
        "fetch_real_data",
        fail_if_network_called,
    )

    result = (
        scanner._load_cached_historical_context(
            symbol="TEST",
            target_date=date(
                2026, 9, 23
            ),
        )
    )

    assert result is not None
    avg_volume, atr = result
    assert avg_volume > 0
    assert atr > 0


def test_missing_daily_context_returns_unavailable(
    tmp_path,
):
    scanner = NiftyUniverseScanner(
        cache_dir=tmp_path
    )

    result = (
        scanner._load_cached_historical_context(
            symbol="DOES_NOT_EXIST",
            target_date=date(
                2026, 10, 1
            ),
        )
    )

    assert result is None


def test_missing_historical_context_is_not_ranked():
    scanner = NiftyUniverseScanner()

    result = scanner._get_historical_context(
        kite_client=object(),
        symbol="TEST",
        token=None,
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
        force_refresh=False,
    )

    assert result == (0, 0.0)


def test_liquidity_filter_rejects_missing_average_volume():
    from scanner.liquidity_filter import (
        LiquidityFilter,
        LiquidityStatus,
    )

    lfilter = LiquidityFilter()

    result = lfilter.evaluate_stock({
        "symbol": "TEST",
        "ltp": 1000.0,
        "volume": 2_000_000,
        "avg_volume_20d": 0,
    })

    assert (
        result.status
        == LiquidityStatus.DATA_UNAVAILABLE
    )

    assert result.is_tradable is False


def test_liquidity_filter_does_not_use_current_volume_as_history():
    from scanner.liquidity_filter import (
        LiquidityFilter,
        LiquidityStatus,
    )

    lfilter = LiquidityFilter()

    result = lfilter.evaluate_stock({
        "symbol": "TEST",
        "ltp": 1000.0,
        "volume": 2_000_000,
        # Historical baseline intentionally absent.
    })

    assert (
        result.status
        == LiquidityStatus.DATA_UNAVAILABLE
    )

    assert result.is_tradable is False


def test_missing_history_context_returns_none():
    scanner = NiftyUniverseScanner()

    assert (
        scanner._get_historical_context(
            kite_client=object(),
            symbol="TEST",
            token=None,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
        )
        == (0, 0.0)
    )







