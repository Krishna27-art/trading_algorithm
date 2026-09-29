"""
Average True Range (ATR) calculation for single-stock volatility normalization.
"""

import numpy as np
import pandas as pd


def calculate_atr(df: pd.DataFrame, period: int = 14, method: str = "wilder") -> pd.Series:
    """Computes Average True Range over given period.
    
    Args:
        df: DataFrame with 'high', 'low', 'close' columns.
        period: Smoothing period (default 14).
        method: 'wilder' for Welles Wilder smoothing (default),
                or 'sma' for simple moving average.
    """
    high = df["high"]
    low = df["low"]
    close_prev = df["close"].shift(1)

    tr1 = high - low
    tr2 = (high - close_prev).abs()
    tr3 = (low - close_prev).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    if method == "sma":
        atr = tr.rolling(window=period, min_periods=period).mean()
    else:
        # Classic Welles Wilder smoothing: exponential moving average with alpha = 1 / period
        atr = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return pd.Series(atr, index=df.index, name=f"atr_{period}")

