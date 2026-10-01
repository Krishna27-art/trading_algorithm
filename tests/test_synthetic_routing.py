import os
import pytest

from scanner.stock_ranker import StockUniverseScanner


def test_scanner_is_real_only_by_default(monkeypatch):
    """
    The production scanner must not enter synthetic mode merely because
    PYTEST_CURRENT_TEST exists.
    """
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/fake_test::test_fake")

    scanner = StockUniverseScanner()

    with pytest.raises(RuntimeError, match="not authenticated"):
        scanner.scan_universe(
            kite_client=None,
            top_n=5,
        )


def test_scanner_synthetic_mode_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/fake_test::test_fake")

    scanner = StockUniverseScanner()

    candidates, data_source = scanner.scan_universe(
        kite_client=None,
        top_n=5,
        allow_synthetic=True,
    )

    assert data_source == "SYNTHETIC"
    assert len(candidates) == 5
