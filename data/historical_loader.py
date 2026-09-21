"""
Real NSE Historical Data Loader.

Also imported by run_algo.py but missing entirely from the repo — that's
why `python run_algo.py --mode backtest` couldn't even start. On top of
just existing, this version actually pulls real NSE candles instead of
inventing them.

Where the data comes from
--------------------------
Zerodha's Kite Connect "Historical API" (self.kite.historical_data) is the
correct, ToS-compliant source here, since this whole project already
authenticates against Kite (see auth.py / kite_client.py). It requires:
  1. A Kite Connect API key with the Historical Data subscription
     (paid add-on, separate from normal trading access) enabled on your
     Zerodha developer console.
  2. The numeric `instrument_token` for the contract you want — NOT the
     trading symbol. Look it up once via kite.instruments("NFO") /
     kite.instruments("NSE") and hardcode it in config, or resolve it with
     resolve_instrument_token() below.

Kite enforces a maximum date range per request that shrinks as the candle
interval gets finer (fetching a year of 15-minute bars in one call will be
rejected). This loader chunks the request into windows the API accepts,
sleeps between calls to stay under the rate limit, and caches the merged
result to a local CSV so you only ever hit the API once per date range —
important both for your API quota and because "practice on real history"
sessions tend to be re-run many times while you tune the strategy.

No lookahead is enforced at the *loading* stage too: every request is
bounded by `to_date`, so nothing later than the range you asked for is ever
in the returned frame. The backtest engines (event_engine.py /
rolling_walk_forward.py) are what actually walk through it bar-by-bar
without peeking ahead.
"""

from __future__ import annotations

import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional, Protocol

import numpy as np
import pandas as pd

from data.market_calendar import MarketCalendar
from monitoring.logger import logger

# Kite's documented max days per request, by interval. Pulling a wider range
# than this in one call gets rejected by the API, not silently truncated.
_MAX_DAYS_PER_REQUEST = {
    "minute": 60,
    "3minute": 100,
    "5minute": 100,
    "10minute": 100,
    "15minute": 200,
    "30minute": 200,
    "60minute": 400,
    "day": 2000,
}


class SupportsHistoricalCandles(Protocol):
    """Structural type — any client with this method works (KiteApp from
    kite_client.py satisfies it), so this module never has to import
    kiteconnect directly."""

    def get_historical_candles(
        self,
        instrument_token: int,
        from_date: str,
        to_date: str,
        interval: str = "15minute",
        continuous: bool = False,
        oi: bool = False,
    ) -> List[dict]: ...


class HistoricalDataLoader:
    @staticmethod
    def load_csv(filepath: Path) -> pd.DataFrame:
        df = pd.read_csv(filepath)
        df.columns = [c.lower().strip() for c in df.columns]

        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"])
        elif "date" in df.columns and "time" in df.columns:
            df["datetime"] = pd.to_datetime(df["date"] + " " + df["time"])
        elif "date" in df.columns:
            df["datetime"] = pd.to_datetime(df["date"])

        df.sort_values("datetime", inplace=True)
        return df

    @staticmethod
    def resolve_instrument_token(
        kite_client: Any, tradingsymbol: str, exchange: str = "NFO"
    ) -> Optional[int]:
        """One-time lookup helper: maps a trading symbol (e.g. 'NIFTY24DECFUT')
        to the numeric instrument_token the historical API needs. Kite's
        instrument dump is large (~90k rows for NFO); cache the token you get
        back in config rather than resolving it on every run."""
        try:
            instruments = kite_client.kite.instruments(exchange)
        except Exception as e:
            logger.error(f"Could not fetch instrument dump for {exchange}: {e}")
            return None

        for inst in instruments:
            if inst.get("tradingsymbol") == tradingsymbol:
                return int(inst["instrument_token"])

        logger.warning(f"No instrument_token found for {tradingsymbol} on {exchange}")
        return None

    @staticmethod
    def fetch_real_data(
        kite_client: SupportsHistoricalCandles,
        instrument_token: int,
        start_date: date,
        end_date: date,
        interval: str = "15minute",
        cache_path: Optional[Path] = None,
        force_refresh: bool = False,
        request_pause_seconds: float = 0.35,
    ) -> pd.DataFrame:
        """
        Downloads real NSE/NFO candles for [start_date, end_date], chunked to
        respect Kite's per-request date-range limits, and returns one clean,
        chronologically sorted DataFrame with columns:
        datetime, open, high, low, close, volume.

        Set cache_path (e.g. Path("data/cache/nifty_15m_2023_2025.csv")) to
        avoid re-downloading on every run — the file is read back and used
        as-is if force_refresh=False and it already covers the requested range.
        """
        if cache_path is not None and cache_path.exists() and not force_refresh:
            cached = pd.read_csv(cache_path, parse_dates=["datetime"])
            cached_start = cached["datetime"].dt.date.min()
            cached_end = cached["datetime"].dt.date.max()
            if cached_start <= start_date and cached_end >= end_date:
                logger.info(f"Using cached historical data from {cache_path} "
                            f"({cached_start} to {cached_end}).")
                mask = (cached["datetime"].dt.date >= start_date) & (cached["datetime"].dt.date <= end_date)
                return cached.loc[mask].reset_index(drop=True)
            logger.info("Cache exists but doesn't cover the requested range — refetching.")

        max_days = _MAX_DAYS_PER_REQUEST.get(interval, 60)
        all_rows: List[dict] = []

        chunk_start = start_date
        while chunk_start <= end_date:
            chunk_end = min(chunk_start + timedelta(days=max_days - 1), end_date)

            logger.info(f"Fetching {interval} candles for token {instrument_token}: "
                        f"{chunk_start} -> {chunk_end}")
            candles = kite_client.get_historical_candles(
                instrument_token=instrument_token,
                from_date=chunk_start.strftime("%Y-%m-%d"),
                to_date=chunk_end.strftime("%Y-%m-%d"),
                interval=interval,
            )
            if candles:
                all_rows.extend(candles)
            else:
                logger.warning(f"No candles returned for {chunk_start} -> {chunk_end} "
                                f"(holiday range, bad token, or missing Historical API subscription?)")

            chunk_start = chunk_end + timedelta(days=1)
            _time.sleep(request_pause_seconds)  # stay well under Kite's rate limit

        if not all_rows:
            raise RuntimeError(
                "Zero candles returned across the whole requested range. Check: "
                "(1) instrument_token is correct, (2) your Kite account has the "
                "paid Historical Data subscription enabled, (3) access_token is "
                "still valid (run auth.py again if it's expired)."
            )

        df = pd.DataFrame(all_rows)
        df.rename(columns={"date": "datetime"}, inplace=True)
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
        df = df[["datetime", "open", "high", "low", "close", "volume"]]
        df.drop_duplicates(subset="datetime", inplace=True)
        df.sort_values("datetime", inplace=True)
        df.reset_index(drop=True, inplace=True)

        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(cache_path, index=False)
            logger.info(f"Cached {len(df)} bars to {cache_path}")

        return df

    @staticmethod
    def generate_synthetic_nifty_data(
        start_date: Optional[datetime] = None,
        days: int = 180,
        base_price: float = 24000.0,
        seed: int = 42,
    ) -> pd.DataFrame:
        """
        Fake data — a random-walk-with-drift generator, kept ONLY so the repo
        still runs end-to-end (`--mode backtest`) for someone who hasn't set
        up Kite API credentials yet. Any performance numbers from this are
        meaningless for evaluating the actual strategy — they reflect the
        random walk's statistical properties, not real NIFTY behavior
        (no real volatility clustering, no real gap opens, no real event
        days). Use fetch_real_data() for anything you intend to draw
        conclusions from.
        """
        if start_date is None:
            start_date = datetime(2025, 1, 1)

        rng = np.random.default_rng(seed)
        bars_per_day = 25  # 09:15 to 15:15 in 15-min bars
        rows = []
        price = base_price
        current_date = start_date

        generated_days = 0
        while generated_days < days:
            if not MarketCalendar.is_trading_day(current_date.date()):
                current_date += timedelta(days=1)
                continue

            day_open = price + rng.normal(0, base_price * 0.001)
            t = current_date.replace(hour=9, minute=15, second=0, microsecond=0)
            for _ in range(bars_per_day):
                drift = rng.normal(0, base_price * 0.0015)
                o = price
                c = price + drift
                h = max(o, c) + abs(rng.normal(0, base_price * 0.0005))
                l = min(o, c) - abs(rng.normal(0, base_price * 0.0005))
                vol = int(abs(rng.normal(150000, 40000)))
                rows.append({"datetime": t, "open": round(o, 2), "high": round(h, 2),
                             "low": round(l, 2), "close": round(c, 2), "volume": vol})
                price = c
                t += timedelta(minutes=15)

            price = day_open + rng.normal(0, base_price * 0.002)  # overnight gap
            generated_days += 1
            current_date += timedelta(days=1)

        return pd.DataFrame(rows)
