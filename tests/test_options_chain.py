"""
Unit tests for OptionsChainLoader and Black-Scholes IV calculation.
Verifies options chain construction, batch quoting, 5-minute spot retrieval, and India VIX series.
"""

from datetime import date, datetime
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from data.options_chain_loader import OptionsChainLoader, calculate_bs_iv


def test_calculate_bs_iv():
    spot = 24000.0
    strike = 24000.0
    tau = 7.0 / 365.0
    rate = 0.065
    price = 150.0  # Realistic ATM option price

    iv_call = calculate_bs_iv(price=price, spot=spot, strike=strike, years_to_expiry=tau, option_type="CE", rate=rate)
    assert 0.05 < iv_call < 0.50

    iv_put = calculate_bs_iv(price=price, spot=spot, strike=strike, years_to_expiry=tau, option_type="PE", rate=rate)
    assert 0.05 < iv_put < 0.50


def test_options_chain_loader_builds_dataframe(tmp_path):
    mock_kite = MagicMock()
    expiry = date(2025, 1, 30)
    exp_str = expiry.strftime("%Y-%m-%d")

    # Mock NFO instruments
    mock_instruments = [
        {
            "tradingsymbol": f"NIFTY25JAN{k}{t}",
            "name": "NIFTY",
            "expiry": exp_str,
            "strike": float(k),
            "instrument_type": t,
            "lot_size": 75,
            "instrument_token": 1000 + idx,
        }
        for idx, (k, t) in enumerate([
            (23800, "PE"), (23900, "PE"), (24000, "PE"), (24100, "PE"), (24200, "PE"),
            (23800, "CE"), (23900, "CE"), (24000, "CE"), (24100, "CE"), (24200, "CE"),
        ])
    ]

    mock_kite.instruments.return_value = mock_instruments

    # Mock batch quote response
    def mock_quote(symbols):
        out = {}
        for sym in symbols:
            out[sym] = {
                "last_price": 95.0,
                "depth": {
                    "buy": [{"price": 94.5}],
                    "sell": [{"price": 95.5}],
                },
            }
        return out

    mock_kite.quote.side_effect = mock_quote

    loader = OptionsChainLoader(kite_client=mock_kite)
    chain = loader.get_nifty_options_chain(
        expiry=expiry,
        spot=24000.0,
        strike_range_pts=500.0,
    )

    assert isinstance(chain, pd.DataFrame)
    required_cols = {"tradingsymbol", "strike", "option_type", "iv", "last_price", "bid", "ask"}
    assert required_cols.issubset(set(chain.columns))
    assert len(chain) == 10
    assert (chain["iv"] > 0).all()
    assert (chain["bid"] == 94.5).all()
    assert (chain["ask"] == 95.5).all()


def test_options_chain_loader_nifty_intraday_and_vix():
    mock_kite = MagicMock()
    loader = OptionsChainLoader(kite_client=mock_kite)

    with pytest.MonkeyPatch.context() as mp:
        mock_df = pd.DataFrame([
            {
                "datetime": datetime(2025, 1, 15, 9, 15),
                "open": 24000.0,
                "high": 24050.0,
                "low": 23980.0,
                "close": 24020.0,
                "volume": 50000,
            },
            {
                "datetime": datetime(2025, 1, 15, 9, 20),
                "open": 24020.0,
                "high": 24040.0,
                "low": 23990.0,
                "close": 24010.0,
                "volume": 40000,
            },
        ])
        mp.setattr(
            "data.historical_loader.HistoricalDataLoader.fetch_real_data",
            lambda *args, **kwargs: mock_df,
        )

        df_5m = loader.fetch_nifty_intraday_5min(as_of=date(2025, 1, 15))
        assert len(df_5m) == 2
        assert "close" in df_5m.columns

        vix_series = loader.fetch_india_vix_series(start_date=date(2025, 1, 1), end_date=date(2025, 1, 15))
        assert isinstance(vix_series, pd.Series)
        assert len(vix_series) == 2
