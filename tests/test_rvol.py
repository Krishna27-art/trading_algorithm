import pandas as pd

from backend.scanner.liquidity_filter import LiquidityFilter, LiquidityStatus
from backend.scanner.stock_ranker import StockUniverseScanner


def test_rvol_definition_is_cumulative_day_volume_divided_by_full_day_average():
    scanner = StockUniverseScanner()

    cumulative_day_volume = 2_500_000
    avg_full_day_volume = 1_000_000

    rvol = round(
        cumulative_day_volume / avg_full_day_volume,
        2,
    )

    assert rvol == 2.50


def test_rvol_does_not_use_current_volume_as_historical_average():
    liquidity = LiquidityFilter()

    result = liquidity.evaluate_stock(
        {
            "symbol": "TEST",
            "ltp": 100.0,
            "volume": 3_000_000,
            # avg_volume_20d intentionally missing
        }
    )

    assert result.status == LiquidityStatus.DATA_UNAVAILABLE
    assert result.is_tradable is False
    assert any(
        "average volume unavailable" in reason.lower()
        for reason in result.rejection_reasons
    )


def test_rvol_missing_average_never_becomes_one():
    liquidity = LiquidityFilter()

    result = liquidity.evaluate_stock(
        {
            "symbol": "TEST",
            "ltp": 100.0,
            "volume": 1_000_000,
            "avg_volume_20d": 0,
        }
    )

    assert result.status == LiquidityStatus.DATA_UNAVAILABLE
    assert result.is_tradable is False


def test_historical_context_requires_20_full_session_days(monkeypatch):
    scanner = StockUniverseScanner()

    dates = pd.date_range("2026-01-01", periods=19, freq="D")

    df = pd.DataFrame(
        {
            "datetime": dates,
            "open": [100.0] * 19,
            "high": [101.0] * 19,
            "low": [99.0] * 19,
            "close": [100.0] * 19,
            "volume": [1_000_000] * 19,
        }
    )

    monkeypatch.setattr(
        "backend.scanner.stock_ranker.HistoricalDataLoader.fetch_real_data",
        lambda **kwargs: df,
    )

    avg_volume, atr = scanner._get_historical_context(
        kite_client=object(),
        symbol="TEST",
        token=123,
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
    )

    assert avg_volume == 0
    assert atr == 0.0


def test_historical_context_uses_exact_previous_20_full_day_average(monkeypatch):
    scanner = StockUniverseScanner()

    dates = pd.date_range("2026-01-01", periods=20, freq="D")
    volumes = list(range(1_000_000, 1_000_020))

    df = pd.DataFrame(
        {
            "datetime": dates,
            "open": [100.0] * 20,
            "high": [101.0] * 20,
            "low": [99.0] * 20,
            "close": [100.0] * 20,
            "volume": volumes,
        }
    )

    monkeypatch.setattr(
        "backend.scanner.stock_ranker.HistoricalDataLoader.fetch_real_data",
        lambda **kwargs: df,
    )

    avg_volume, _ = scanner._get_historical_context(
        kite_client=object(),
        symbol="TEST",
        token=123,
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
    )

    assert avg_volume == int(round(sum(volumes) / 20))
