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

import os
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple

import numpy as np
import pandas as pd

from backend.data.time_utils import MarketCalendar
from backend.data.time_utils import now_ist_iso, now_ist_naive, to_ist_naive
from backend.monitoring.logger import logger

IST = "Asia/Kolkata"


def _normalize_timestamp_to_ist_naive(value: Any) -> Optional[datetime]:
    return to_ist_naive(value)


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
            df["datetime"] = df["datetime"].map(_normalize_timestamp_to_ist_naive)
        elif "date" in df.columns and "time" in df.columns:
            combined = df["date"].astype(str) + " " + df["time"].astype(str)
            df["datetime"] = combined.map(_normalize_timestamp_to_ist_naive)
        elif "date" in df.columns:
            df["datetime"] = df["date"].map(_normalize_timestamp_to_ist_naive)

        if "datetime" not in df.columns or df["datetime"].isna().any():
            raise ValueError(f"CSV contains invalid or missing timestamps: {filepath}")

        df.sort_values("datetime", inplace=True)
        df.reset_index(drop=True, inplace=True)
        is_valid, errors = HistoricalDataLoader.validate_candles(df)
        if not is_valid:
            raise ValueError(f"CSV candle validation failed for {filepath}: {'; '.join(errors)}")
        return df

    @staticmethod
    def resolve_instrument_token(
        kite_client: Any, tradingsymbol: str, exchange: str = "NFO"
    ) -> Optional[int]:
        from backend.data.instrument_resolver import resolve_token
        return resolve_token(kite_client, tradingsymbol, exchange)

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
            try:
                cached, metadata = (
                    HistoricalDataLoader
                    .load_cached_data_with_validation(cache_path)
                )

                if cached.empty:
                    raise ValueError(
                        f"Cached historical data is empty: {cache_path}"
                    )

                if metadata is None:
                    raise ValueError(
                        f"Cached metadata is missing: {cache_path}"
                    )

                cached_interval = metadata.get("interval")
                if cached_interval != interval:
                    raise ValueError(
                        f"Cached interval mismatch: "
                        f"cached={cached_interval!r}, expected={interval!r}"
                    )

                cached_token = metadata.get("instrument_token")
                if cached_token is not None:
                    try:
                        if int(cached_token) != int(instrument_token):
                            raise ValueError(
                                f"Cached instrument token mismatch: "
                                f"cached={cached_token!r}, "
                                f"expected={instrument_token!r}"
                            )
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f"Invalid cached instrument token metadata: "
                            f"{cached_token!r}"
                        ) from exc

                cached_start = cached["datetime"].dt.date.min()
                cached_end = cached["datetime"].dt.date.max()

                if cached_start <= start_date and cached_end >= end_date:
                    logger.info(
                        f"Using validated cached historical data from "
                        f"{cache_path} ({cached_start} to {cached_end})."
                    )

                    mask = (
                        (cached["datetime"].dt.date >= start_date)
                        & (cached["datetime"].dt.date <= end_date)
                    )

                    result = cached.loc[mask].copy()
                    result = (
                        result
                        .sort_values("datetime")
                        .reset_index(drop=True)
                    )

                    return result

                logger.info(
                    "Validated cache exists but does not cover the "
                    "requested range — refetching."
                )

            except Exception as exc:
                logger.warning(
                    f"Cached historical data rejected: "
                    f"{cache_path}: {exc}. Refetching from Kite."
                )

        max_days = _MAX_DAYS_PER_REQUEST.get(interval, 60)
        all_rows: List[dict] = []

        chunk_start = start_date
        while chunk_start <= end_date:
            chunk_end = min(chunk_start + timedelta(days=max_days - 1), end_date)

            logger.info(f"Fetching {interval} candles for token {instrument_token}: "
                        f"{chunk_start} -> {chunk_end}")
            try:
                if hasattr(kite_client, "get_historical_candles"):
                    candles = kite_client.get_historical_candles(
                        instrument_token=instrument_token,
                        from_date=chunk_start.strftime("%Y-%m-%d"),
                        to_date=chunk_end.strftime("%Y-%m-%d"),
                        interval=interval,
                    )
                elif hasattr(kite_client, "historical_data"):
                    candles = kite_client.historical_data(
                        instrument_token=instrument_token,
                        from_date=chunk_start.strftime("%Y-%m-%d"),
                        to_date=chunk_end.strftime("%Y-%m-%d"),
                        interval=interval,
                    )
                else:
                    raise TypeError(f"Client {type(kite_client)} does not support historical data retrieval.")
            except Exception as e:
                err_type = type(e).__name__
                err_msg = str(e)
                logger.error(
                    f"Kite Historical API call failed: {err_type}: {err_msg} | "
                    f"token={instrument_token}, interval={interval}, from={chunk_start}, to={chunk_end}"
                )
                raise RuntimeError(
                    f"Kite Historical API ({err_type}): {err_msg} "
                    f"[token={instrument_token}, interval={interval}, dates={chunk_start} to {chunk_end}]"
                ) from e

            if candles:
                all_rows.extend(candles)
            else:
                expected_days = sum(
                    1 for d in range((chunk_end - chunk_start).days + 1)
                    if MarketCalendar.is_trading_day(chunk_start + timedelta(days=d))
                )
                if expected_days > 0 and not all_rows:
                    logger.warning(
                        f"No candles returned for {chunk_start} -> {chunk_end} "
                        f"(expected {expected_days} trading days)"
                    )

            chunk_start = chunk_end + timedelta(days=1)
            _time.sleep(request_pause_seconds)

        if not all_rows:
            raise RuntimeError(
                f"Zero candles returned across {start_date} to {end_date} for token {instrument_token}. Check: "
                "(1) instrument_token is correct, (2) Kite account has the paid Historical Data "
                "API subscription active, (3) access token is valid."
            )

        df = pd.DataFrame(all_rows)
        df.rename(columns={"date": "datetime"}, inplace=True)
        df["datetime"] = (
            df["datetime"]
            .map(_normalize_timestamp_to_ist_naive)
        )

        if df["datetime"].isna().any():
            raise ValueError(
                "Historical data contains invalid/unconvertible timestamps."
            )
        cols = ["datetime", "open", "high", "low", "close", "volume"]
        if "oi" in df.columns:
            cols.append("oi")
        df = df[cols]
        df.sort_values("datetime", inplace=True)
        df.reset_index(drop=True, inplace=True)

        raw_dup_count = int(
            df["datetime"].duplicated().sum()
        )
        if raw_dup_count > 0:
            duplicated_timestamps = (
                df.loc[
                    df["datetime"].duplicated(
                        keep=False
                    ),
                    "datetime",
                ]
                .drop_duplicates()
                .tolist()
            )
            logger.error(
                "Historical data for token %s contains "
                "duplicate timestamp(s): %s",
                instrument_token,
                duplicated_timestamps,
            )
            raise ValueError(
                "Historical data contains "
                f"{raw_dup_count} duplicate timestamp(s): "
                f"{duplicated_timestamps}"
            )

        is_valid, errors = HistoricalDataLoader.validate_candles(df)
        if not is_valid:
            raise ValueError(f"Historical market data validation failed: {'; '.join(errors)}")

        if cache_path is not None:
            metadata = {
                "symbol": cache_path.stem.split("_")[0] if cache_path else "UNKNOWN",
                "instrument_token": instrument_token,
                "interval": interval,
                "start_date": str(start_date),
                "end_date": str(end_date),
                "source": "Zerodha Kite Connect Historical API",
                "downloaded_timestamp": now_ist_iso(),
                "candle_count": len(df),
                "trading_days": int(df["datetime"].dt.date.nunique()),
            }
            HistoricalDataLoader.save_with_metadata(df, cache_path, metadata)

        return df

    @staticmethod
    def validate_candles(df: pd.DataFrame) -> Tuple[bool, List[str]]:
        errors: List[str] = []
        required_cols = {"datetime", "open", "high", "low", "close", "volume"}
        if not required_cols.issubset(df.columns):
            missing = required_cols - set(df.columns)
            errors.append(f"Missing required columns: {missing}")
            return False, errors

        if len(df) == 0:
            errors.append("Dataset contains 0 candles.")
            return False, errors

        if df[list(required_cols)].isna().any().any():
            null_cols = df[list(required_cols)].columns[df[list(required_cols)].isna().any()].tolist()
            errors.append(f"Null values detected in columns: {null_cols}")

        for col in ["open", "high", "low", "close", "volume"]:
            if not np.isfinite(df[col]).all():
                errors.append(f"Non-finite values (NaN or Inf) detected in {col}.")

        if (df["open"] <= 0).any() or (df["high"] <= 0).any() or (df["low"] <= 0).any() or (df["close"] <= 0).any():
            errors.append("Non-positive prices found in OHLC data.")

        if (df["volume"] < 0).any():
            errors.append("Negative volume found in dataset.")

        invalid_high = (df["high"] < df["low"]) | (df["high"] < df["open"]) | (df["high"] < df["close"])
        if invalid_high.any():
            bad_count = invalid_high.sum()
            errors.append(f"Found {bad_count} candles where High is lower than Open, Low, or Close.")

        invalid_low = (df["low"] > df["high"]) | (df["low"] > df["open"]) | (df["low"] > df["close"])
        if invalid_low.any():
            bad_count = invalid_low.sum()
            errors.append(f"Found {bad_count} candles where Low is higher than Open, High, or Close.")

        if not df["datetime"].is_monotonic_increasing:
            errors.append("Timestamps are out of chronological order.")

        dup_count = df["datetime"].duplicated().sum()
        if dup_count > 0:
            errors.append(f"Found {dup_count} duplicate timestamps.")

        is_intraday = (df["datetime"].dt.time != time(0, 0)).any()
        if is_intraday:
            if (df["datetime"].dt.weekday >= 5).any():
                errors.append("Weekend candles detected in dataset.")

        return len(errors) == 0, errors

    @staticmethod
    def save_with_metadata(df: pd.DataFrame, csv_path: Path, metadata: Dict[str, Any]):
        import json
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_csv = csv_path.with_name(f"{csv_path.name}.tmp")
        df.to_csv(tmp_csv, index=False)
        with open(tmp_csv, "a") as f:
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_csv, csv_path)

        meta_path = csv_path.with_suffix(".meta.json")
        tmp_meta = meta_path.with_name(f"{meta_path.name}.tmp")
        with open(tmp_meta, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_meta, meta_path)
        logger.info(f"Saved {len(df)} validated bars and metadata to {csv_path}")

    @staticmethod
    def load_cached_data_with_validation(csv_path: Path) -> Tuple[pd.DataFrame, Optional[Dict[str, Any]]]:
        import json
        if not csv_path.exists():
            raise FileNotFoundError(f"Cache file {csv_path} does not exist.")

        meta_path = csv_path.with_suffix(".meta.json")
        meta = None
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception as e:
                raise ValueError(f"Corrupt cache metadata for {csv_path}: {e}")

        df = pd.read_csv(csv_path)
        if "datetime" not in df.columns:
            raise ValueError(f"Missing datetime column in {csv_path}")

        df["datetime"] = df["datetime"].map(_normalize_timestamp_to_ist_naive)
        if df["datetime"].isna().any():
            raise ValueError(f"Invalid timestamps in cache {csv_path}")

        if df["datetime"].duplicated().any():
            raise ValueError(f"Corrupt cache: duplicate timestamps in {csv_path}")

        is_valid, errors = HistoricalDataLoader.validate_candles(df)
        if not is_valid:
            raise ValueError(f"Cached data validation failed for {csv_path}: {'; '.join(errors)}")

        return df, meta

    @staticmethod
    def get_latest_completed_candle_start(
        now: datetime,
        interval_minutes: int = 15,
        session_open: time = time(9, 15),
        session_close: time = time(15, 30),
    ) -> Optional[datetime]:
        """
        Return the start timestamp of the latest COMPLETED intraday candle.

        Kite 15-minute candles are timestamped by candle start:
            09:15 -> candle covering 09:15-09:30
            09:30 -> candle covering 09:30-09:45
            ...

        Therefore at:
            10:07 -> latest completed candle starts at 09:45
            10:15 -> latest completed candle starts at 10:00
            14:07 -> latest completed candle starts at 13:45
            15:30 -> latest completed candle starts at 15:15

        The currently-forming candle is NEVER considered completed.
        """

        if interval_minutes <= 0:
            raise ValueError("interval_minutes must be > 0")

        now = to_ist_naive(now)
        if now is None:
            raise ValueError("Invalid timestamp provided to get_latest_completed_candle_start")

        session_start = datetime.combine(now.date(), session_open)
        session_end = datetime.combine(now.date(), session_close)

        first_completed = session_start + timedelta(
            minutes=interval_minutes
        )

        if now < first_completed:
            return None

        effective_now = min(now, session_end)

        elapsed_minutes = (
            effective_now - session_start
        ).total_seconds() / 60.0

        completed_bars = int(
            elapsed_minutes // interval_minutes
        )

        if completed_bars <= 0:
            return None

        return session_start + timedelta(
            minutes=(completed_bars - 1) * interval_minutes
        )

    @staticmethod
    def load_or_refresh_intraday_cache(
        kite_client: Any,
        instrument_token: int,
        cache_path: Path,
        now: Optional[datetime] = None,
        lookback_days: int = 45,
        interval: str = "15minute",
        request_pause_seconds: float = 0.35,
    ) -> pd.DataFrame:
        interval_minutes_map = {
            "minute": 1,
            "15minute": 15,
        }
        interval_minutes = interval_minutes_map.get(interval)
        if interval_minutes is None:
            raise ValueError(
                "Unsupported intraday interval. "
                "Supported: 'minute', '15minute'."
            )

        if kite_client is None:
            raise RuntimeError(
                "Kite client is required for live intraday data."
            )

        now = to_ist_naive(now or now_ist_naive())
        if now is None:
            raise ValueError("Invalid current time for intraday cache refresh")
        today = now.date()

        latest_completed = (
            HistoricalDataLoader.get_latest_completed_candle_start(
                now=now,
                interval_minutes=interval_minutes,
            )
        )

        if latest_completed is None:
            prev_session = MarketCalendar.previous_trading_session(today)
            prev_close_dt = datetime.combine(prev_session, time(15, 30))
            if cache_path.exists():
                try:
                    cached, meta = (
                        HistoricalDataLoader
                        .load_cached_data_with_validation(cache_path)
                    )
                    if not cached.empty and meta is not None:
                        if int(meta.get("instrument_token", 0)) == int(instrument_token):
                            cached = cached[cached["datetime"] <= prev_close_dt]
                            if not cached.empty:
                                return cached.sort_values("datetime").reset_index(drop=True)
                except Exception as exc:
                    logger.exception(
                        "Pre-market historical cache read failed for token %s: %s",
                        instrument_token,
                        exc,
                    )
            try:
                start_date = today - timedelta(days=int(lookback_days))
                fresh = HistoricalDataLoader.fetch_real_data(
                    kite_client=kite_client,
                    instrument_token=instrument_token,
                    start_date=start_date,
                    end_date=prev_session,
                    interval=interval,
                    cache_path=cache_path,
                    force_refresh=True,
                    request_pause_seconds=request_pause_seconds,
                )
                return fresh
            except Exception as exc:
                logger.exception(
                    "Pre-market historical Kite fetch failed for token %s: %s",
                    instrument_token,
                    exc,
                )
                raise

        cached = pd.DataFrame()

        if cache_path.exists():
            try:
                cached, meta = (
                    HistoricalDataLoader
                    .load_cached_data_with_validation(cache_path)
                )

                if not cached.empty and meta is not None:
                    if int(meta.get("instrument_token", 0)) != int(instrument_token):
                        raise ValueError(
                            f"Cache token mismatch: {meta.get('instrument_token')} != {instrument_token}"
                        )
                    if meta.get("interval") != interval:
                        raise ValueError(
                            f"Cache interval mismatch: {meta.get('interval')} != {interval}"
                        )
                    cached = cached.sort_values("datetime").reset_index(drop=True)

            except Exception as exc:
                logger.warning(
                    f"Invalid intraday cache {cache_path}: {exc}. "
                    "Rebuilding from Kite."
                )
                cached = pd.DataFrame()

        if not cached.empty:
            cached = cached[
                cached["datetime"] <= latest_completed
            ].copy()

            cached = cached.sort_values(
                "datetime"
            ).reset_index(drop=True)

        if (
            not cached.empty
            and cached["datetime"].max() >= latest_completed
        ):
            return cached

        if cached.empty:
            start_date = today - timedelta(
                days=int(lookback_days)
            )
        else:
            cached_max_date = cached["datetime"].max().date()
            start_date = cached_max_date

        logger.info(
            f"Refreshing intraday cache for token "
            f"{instrument_token}: {start_date} -> {today}, "
            f"latest completed candle={latest_completed}"
        )

        try:
            fresh = HistoricalDataLoader.fetch_real_data(
                kite_client=kite_client,
                instrument_token=instrument_token,
                start_date=start_date,
                end_date=today,
                interval=interval,
                cache_path=None,
                force_refresh=True,
                request_pause_seconds=request_pause_seconds,
            )
        except Exception as exc:
            logger.error(
                f"Incremental Kite fetch failed for token "
                f"{instrument_token}: {exc}"
            )
            raise RuntimeError(
                "Live intraday refresh failed and the existing "
                "cache is behind the latest completed candle for "
                f"token {instrument_token}."
            ) from exc

        if fresh.empty:
            raise RuntimeError(
                "Kite returned no fresh completed candles for "
                f"token {instrument_token}; stale cache will not "
                "be used as a live substitute."
            )

        fresh = fresh[
            fresh["datetime"] <= latest_completed
        ].copy()

        if cached.empty:
            merged = fresh.copy()
        else:
            merged = pd.concat(
                [cached, fresh],
                ignore_index=True,
            )

        if merged.empty:
            raise RuntimeError(
                f"No completed intraday candles available for "
                f"token {instrument_token}."
            )

        merged = (
            merged
            .drop_duplicates(
                subset="datetime",
                keep="last",
            )
            .sort_values("datetime")
            .reset_index(drop=True)
        )

        merged = merged[
            merged["datetime"] <= latest_completed
        ].reset_index(drop=True)

        is_valid, errors = (
            HistoricalDataLoader.validate_candles(merged)
        )

        if not is_valid:
            raise ValueError(
                "Refreshed intraday cache failed validation: "
                + "; ".join(errors)
            )

        metadata = {
            "symbol": cache_path.stem.split("_")[0],
            "instrument_token": instrument_token,
            "interval": interval,
            "start_date": str(
                merged["datetime"].dt.date.min()
            ),
            "end_date": str(
                merged["datetime"].dt.date.max()
            ),
            "source": "Zerodha Kite Connect Historical API",
            "downloaded_timestamp": now_ist_iso(),
            "latest_completed_candle": latest_completed.isoformat(),
            "candle_count": len(merged),
            "trading_days": int(
                merged["datetime"].dt.date.nunique()
            ),
        }

        HistoricalDataLoader.save_with_metadata(
            merged,
            cache_path,
            metadata,
        )

        return merged

    @staticmethod
    def generate_synthetic_nifty_data(
        start_date: Optional[datetime] = None,
        days: int = 180,
        base_price: float = 24000.0,
        seed: int = 42,
    ) -> pd.DataFrame:
        env = os.getenv("TRADING_ENVIRONMENT", "").lower()
        if env in ("production", "prod", "live"):
            raise RuntimeError("Synthetic market data is strictly forbidden in production.")

        if start_date is None:
            start_date = datetime(2025, 1, 1)

        rng = np.random.default_rng(seed)
        bars_per_day = 25
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

            price = day_open + rng.normal(0, base_price * 0.002)
            generated_days += 1
            current_date += timedelta(days=1)

        df = pd.DataFrame(rows)
        if not df.empty:
            from backend.indicators.vwap import calculate_session_vwap
            df["vwap"] = calculate_session_vwap(df).to_numpy()
        return df