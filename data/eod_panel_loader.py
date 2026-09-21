"""
Cross-Sectional Daily OHLCV Panel Loader for Portfolio Strategies.

Uses the existing HistoricalDataLoader and Kite Connect / KiteApp client.
Pulls daily candles (interval="day") chunked to Kite's max request limits,
resolves instrument tokens dynamically via instrument_resolver,
caches per-symbol daily CSVs locally, and constructs wide DataFrames
(index=date, columns=tradingsymbol) for close, high, low, volume,
plus a NIFTY 50 benchmark close Series.
Missing/partial histories are preserved as NaN (never forward-filled or dropped).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from config.settings import settings
from data.historical_loader import HistoricalDataLoader
from data.instrument_resolver import instrument_resolver
from monitoring.logger import logger


class EODPanelLoader:
    """Loads and aligns cross-sectional daily market data into wide panels."""

    def __init__(
        self,
        kite_client: Optional[Any] = None,
        cache_dir: Optional[Path] = None,
    ):
        self.kite_client = kite_client
        self.cache_dir = cache_dir or (settings.base_dir / "data" / "cache" / "daily")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def fetch_symbol_daily(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        token: Optional[int] = None,
        exchange: str = "NSE",
        force_refresh: bool = False,
    ) -> Optional[pd.DataFrame]:
        """
        Fetches daily candles for a single symbol using HistoricalDataLoader.
        Returns a DataFrame with columns: datetime, open, high, low, close, volume.
        """
        resolved_token = token
        if resolved_token is None:
            resolved_token = instrument_resolver.resolve_token(
                symbol=symbol,
                exchange=exchange,
                kite_client=self.kite_client,
            )

        if resolved_token is None:
            logger.warning(f"Could not resolve instrument_token for {symbol} on {exchange}.")
            return None

        cache_file = self.cache_dir / f"{symbol.upper()}_day.csv"

        try:
            df = HistoricalDataLoader.fetch_real_data(
                kite_client=self.kite_client,
                instrument_token=resolved_token,
                start_date=start_date,
                end_date=end_date,
                interval="day",
                cache_path=cache_file,
                force_refresh=force_refresh,
            )
            if df is not None and not df.empty:
                df["datetime"] = pd.to_datetime(df["datetime"])
                df["date"] = df["datetime"].dt.date
                df = df.sort_values("date").drop_duplicates(subset=["date"]).reset_index(drop=True)
                return df
        except Exception as e:
            logger.warning(f"Failed to fetch daily candles for {symbol} (token {resolved_token}): {e}")

        return None

    def load_panel(
        self,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
        index_symbol: str = "NIFTY",
        force_refresh: bool = False,
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series]:
        """
        Builds wide DataFrames (index=date, columns=symbols) for closes, highs,
        lows, and volumes, plus an index_close pd.Series on the exact same date index.

        Symbols with partial or missing history have NaN values in those sessions,
        strictly preserving real data missingness without dropping the column
        or forward-filling prices.
        """
        symbol_dfs: Dict[str, pd.DataFrame] = {}

        # 1. Fetch benchmark index first (e.g. NIFTY 50, token 256265)
        index_token = instrument_resolver.resolve_token(
            symbol=index_symbol,
            exchange="NSE",
            kite_client=self.kite_client,
        ) or 256265

        index_df = self.fetch_symbol_daily(
            symbol=index_symbol,
            start_date=start_date,
            end_date=end_date,
            token=index_token,
            force_refresh=force_refresh,
        )

        if index_df is None or index_df.empty:
            raise RuntimeError(
                f"Benchmark index {index_symbol} historical data could not be retrieved "
                f"between {start_date} and {end_date}."
            )

        # Base calendar is defined by benchmark trading sessions
        all_dates = pd.to_datetime(index_df["date"]).sort_values().drop_duplicates()
        date_index = pd.DatetimeIndex(all_dates)

        index_series = pd.Series(
            data=index_df["close"].to_numpy(dtype=float),
            index=date_index,
            name="index_close",
        )

        # 2. Fetch all constituent symbols
        for sym in symbols:
            df_sym = self.fetch_symbol_daily(
                symbol=sym,
                start_date=start_date,
                end_date=end_date,
                force_refresh=force_refresh,
            )
            if df_sym is not None and not df_sym.empty:
                symbol_dfs[sym] = df_sym

        # 3. Construct wide frames aligned to date_index
        # All symbols from input `symbols` are preserved as columns
        close_dict: Dict[str, pd.Series] = {}
        high_dict: Dict[str, pd.Series] = {}
        low_dict: Dict[str, pd.Series] = {}
        volume_dict: Dict[str, pd.Series] = {}

        for sym in symbols:
            if sym in symbol_dfs:
                s_df = symbol_dfs[sym]
                s_dates = pd.DatetimeIndex(pd.to_datetime(s_df["date"]))
                close_dict[sym] = pd.Series(s_df["close"].to_numpy(dtype=float), index=s_dates)
                high_dict[sym] = pd.Series(s_df["high"].to_numpy(dtype=float), index=s_dates)
                low_dict[sym] = pd.Series(s_df["low"].to_numpy(dtype=float), index=s_dates)
                volume_dict[sym] = pd.Series(s_df["volume"].to_numpy(dtype=float), index=s_dates)
            else:
                # Symbol has zero history -> column of NaNs
                close_dict[sym] = pd.Series(np.nan, index=date_index)
                high_dict[sym] = pd.Series(np.nan, index=date_index)
                low_dict[sym] = pd.Series(np.nan, index=date_index)
                volume_dict[sym] = pd.Series(np.nan, index=date_index)

        # Reindex to master benchmark calendar without forward-filling (leave NaNs)
        closes = pd.DataFrame(close_dict).reindex(date_index)
        highs = pd.DataFrame(high_dict).reindex(date_index)
        lows = pd.DataFrame(low_dict).reindex(date_index)
        volumes = pd.DataFrame(volume_dict).reindex(date_index)

        return closes, highs, lows, volumes, index_series
