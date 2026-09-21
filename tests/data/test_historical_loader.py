"""
Unit Tests for HistoricalDataLoader and Real Data Enforcement.
"""

from datetime import datetime
from pathlib import Path
import pytest

from config.settings import InstrumentConfig, InstrumentType
from data.historical_loader import HistoricalDataLoader
from run_algo import load_history


def test_real_data_required_fails_when_unavailable(tmp_path, monkeypatch):
    """
    REAL_DATA_REQUIRED_TEST:
    If real data is unavailable, the system must say:
    'REAL HISTORICAL DATA REQUIRED — BACKTEST NOT EXECUTED'
    and must not generate performance metrics.
    """
    fake_inst = InstrumentConfig(
        symbol="NONEXISTENT_XYZ",
        exchange="NSE",
        instrument_type=InstrumentType.EQUITY,
        lot_size=1,
        instrument_token=99999999,
    )

    with pytest.raises(RuntimeError) as exc:
        load_history(
            days=30,
            start_date=datetime(2025, 1, 1),
            instrument=fake_inst,
            allow_synthetic=False,
        )

    assert "REAL HISTORICAL DATA REQUIRED — BACKTEST NOT EXECUTED" in str(exc.value)


def test_metadata_persistence(tmp_path):
    csv_file = tmp_path / "TEST_15m_test.csv"
    base = datetime(2025, 1, 1, 9, 15)
    df = pd_df = HistoricalDataLoader.generate_synthetic_nifty_data(days=2, base_price=24000.0)

    metadata = {
        "symbol": "TEST",
        "instrument_token": 12345,
        "interval": "15minute",
        "start_date": "2025-01-01",
        "end_date": "2025-01-02",
        "source": "Zerodha Kite Connect Historical API",
        "downloaded_timestamp": datetime.now().isoformat(),
        "candle_count": len(df),
        "trading_days": 2,
    }

    HistoricalDataLoader.save_with_metadata(df, csv_file, metadata)
    loaded_df, loaded_meta = HistoricalDataLoader.load_cached_data_with_validation(csv_file)

    assert len(loaded_df) == len(df)
    assert loaded_meta["symbol"] == "TEST"
    assert loaded_meta["instrument_token"] == 12345
    assert loaded_meta["candle_count"] == len(df)
