"""
Intraday Session-Anchored Volume Weighted Average Price (VWAP).
Formula:
    Price_typ = (High + Low + Close) / 3
    VWAP = sum(Price_typ * Volume) / sum(Volume)
"""

import numpy as np
import pandas as pd


def calculate_session_vwap(df: pd.DataFrame) -> pd.Series:
    """
    Computes session VWAP resetting every trading day at 09:15 IST.
    Assumes df has datetime index or 'datetime' column and ['high', 'low', 'close', 'volume'].
    """
    data = df.copy()
    if "datetime" in data.columns and not isinstance(data.index, pd.DatetimeIndex):
        data.set_index("datetime", inplace=True)

    typical_price = (data["high"] + data["low"] + data["close"]) / 3.0
    pv = typical_price * data["volume"]

    # Group by calendar date to reset VWAP at each session open
    dates = data.index.date
    cum_pv = pv.groupby(dates).cumsum()
    cum_vol = data["volume"].groupby(dates).cumsum()

    vwap = cum_pv / np.where(cum_vol == 0, 1.0, cum_vol)
    return pd.Series(vwap, index=data.index, name="vwap")
