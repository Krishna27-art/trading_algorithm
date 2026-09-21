"""
Options Chain and Volatility Feed Loader for VRP Strategy.

Fetches:
1. Weekly NIFTY options chain DataFrame matching VRPHarvestStrategy.generate_plan
   (tradingsymbol, strike, option_type, iv, last_price, bid, ask).
   Batches Kite quote API calls (within 500 items/request limit and 3 req/sec rate limit).
2. 5-minute NIFTY 50 spot candles to compute Parkinson realized volatility.
3. India VIX daily close series.
"""

from __future__ import annotations

import math
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config.settings import settings
from data.historical_loader import HistoricalDataLoader
from data.instrument_resolver import instrument_resolver
from monitoring.logger import logger


def calculate_bs_iv(
    price: float,
    spot: float,
    strike: float,
    years_to_expiry: float,
    option_type: str,
    rate: float = 0.065,
) -> float:
    """
    Inverts Black-Scholes formula using Newton-Raphson / bisection
    to find implied volatility. Returns annualized IV as a decimal (e.g. 0.15 for 15%).
    """
    if price <= 0 or spot <= 0 or strike <= 0 or years_to_expiry <= 0:
        return 0.15  # Reasonable fallback

    is_call = option_type.upper() == "CE"
    intrinsic = max(0.0, (spot - strike) if is_call else (strike - spot))
    if price < intrinsic:
        return 0.10

    # Initial guess via Corrado-Miller or standard 20%
    sigma = 0.20
    sqrt_t = math.sqrt(years_to_expiry)

    for _ in range(25):
        d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * years_to_expiry) / (sigma * sqrt_t)
        d2 = d1 - sigma * sqrt_t
        n_d1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
        n_d2 = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))

        if is_call:
            theo = spot * n_d1 - strike * math.exp(-rate * years_to_expiry) * n_d2
        else:
            theo = strike * math.exp(-rate * years_to_expiry) * (1.0 - n_d2) - spot * (1.0 - n_d1)

        diff = theo - price
        if abs(diff) < 1e-4:
            return max(0.01, sigma)

        # Vega
        vega = spot * sqrt_t * (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * d1 * d1)
        if vega < 1e-8:
            break

        step = diff / vega
        sigma -= step
        if sigma <= 0.001 or sigma > 3.0:
            break

    # Fallback to bounded bisection if Newton did not converge
    low, high = 0.01, 2.50
    for _ in range(30):
        mid = (low + high) / 2.0
        d1 = (math.log(spot / strike) + (rate + 0.5 * mid * mid) * years_to_expiry) / (mid * sqrt_t)
        d2 = d1 - mid * sqrt_t
        n_d1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
        n_d2 = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
        theo = (spot * n_d1 - strike * math.exp(-rate * years_to_expiry) * n_d2) if is_call else (
            strike * math.exp(-rate * years_to_expiry) * (1.0 - n_d2) - spot * (1.0 - n_d1)
        )
        if abs(theo - price) < 1e-3:
            return mid
        if theo > price:
            high = mid
        else:
            low = mid

    return max(0.01, (low + high) / 2.0)


class OptionsChainLoader:
    """Feed loader for VRP Iron Condor options chains and volatility data."""

    def __init__(self, kite_client: Optional[Any] = None):
        self.kite_client = kite_client

    def get_nifty_options_chain(
        self,
        expiry: date,
        spot: float,
        rate: float = 0.065,
        strike_range_pts: float = 1200.0,
    ) -> pd.DataFrame:
        """
        Builds the options chain DataFrame expected by VRPHarvestStrategy.generate_plan:
        Columns: [tradingsymbol, strike, option_type, iv, last_price, bid, ask]

        Batches quotes in chunks to respect Kite Connect's 3 req/sec limit.
        """
        if self.kite_client is None:
            raise RuntimeError("Kite client required to fetch live options chain.")

        # 1. Fetch NFO instruments dump
        if hasattr(self.kite_client, "instruments"):
            raw_client = self.kite_client
        elif hasattr(self.kite_client, "kite"):
            raw_client = self.kite_client.kite
        else:
            raw_client = self.kite_client

        instruments = []
        if hasattr(raw_client, "instruments"):
            try:
                instruments = raw_client.instruments("NFO")
            except Exception as e:
                logger.debug(f"Direct instruments call failed: {e}")
        if not instruments:
            instruments = instrument_resolver.get_instruments(raw_client, exchange="NFO")

        exp_str = expiry.strftime("%Y-%m-%d")
        min_k = spot - strike_range_pts
        max_k = spot + strike_range_pts

        candidates = []
        for inst in instruments:
            name = (inst.get("name") or "").strip().upper()
            inst_type = (inst.get("instrument_type") or "").strip().upper()
            inst_exp = str(inst.get("expiry") or "")[:10]
            strike = float(inst.get("strike") or 0.0)

            if name == "NIFTY" and inst_type in ("CE", "PE") and inst_exp == exp_str:
                if min_k <= strike <= max_k:
                    candidates.append({
                        "tradingsymbol": inst["tradingsymbol"],
                        "strike": strike,
                        "option_type": inst_type,
                        "instrument_token": inst.get("instrument_token"),
                    })

        if not candidates:
            logger.warning(f"No NIFTY options found for expiry {exp_str} in [{min_k}, {max_k}].")
            return pd.DataFrame(columns=["tradingsymbol", "strike", "option_type", "iv", "last_price", "bid", "ask"])

        # 2. Batch quote calls
        batch_size = 200
        quote_results: Dict[str, dict] = {}
        all_symbols = [f"NFO:{c['tradingsymbol']}" for c in candidates]

        for i in range(0, len(all_symbols), batch_size):
            chunk = all_symbols[i : i + batch_size]
            try:
                chunk_quotes = raw_client.quote(chunk)
                if chunk_quotes:
                    quote_results.update(chunk_quotes)
            except Exception as e:
                logger.error(f"Error fetching batch quotes for chunk {i}: {e}")
            _time.sleep(0.35)  # Stay safely below 3 requests/sec

        # 3. Assemble options chain rows
        today = date.today()
        tau = max((expiry - today).days, 1) / 365.0

        rows = []
        for cand in candidates:
            sym = cand["tradingsymbol"]
            q_key = f"NFO:{sym}"
            q = quote_results.get(q_key, {})

            ltp = float(q.get("last_price") or 0.0)
            depth = q.get("depth", {})
            buy_depth = depth.get("buy", [])
            sell_depth = depth.get("sell", [])

            bid = float(buy_depth[0].get("price", 0.0)) if buy_depth else ltp
            ask = float(sell_depth[0].get("price", 0.0)) if sell_depth else ltp

            # Prefer mid-price for IV calculation
            mid_price = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else ltp

            # Extract or compute IV
            iv = 0.0
            if "implied_volatility" in q and q["implied_volatility"]:
                iv = float(q["implied_volatility"])
            else:
                iv = calculate_bs_iv(
                    price=mid_price,
                    spot=spot,
                    strike=cand["strike"],
                    years_to_expiry=tau,
                    option_type=cand["option_type"],
                    rate=rate,
                )

            rows.append({
                "tradingsymbol": sym,
                "strike": cand["strike"],
                "option_type": cand["option_type"],
                "iv": round(iv, 4),
                "last_price": round(ltp, 2),
                "bid": round(bid, 2),
                "ask": round(ask, 2),
            })

        df = pd.DataFrame(rows)
        return df.sort_values(by=["strike", "option_type"]).reset_index(drop=True)

    def fetch_nifty_intraday_5min(
        self,
        as_of: date,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """
        Fetches 5-minute NIFTY spot candles for session as_of
        to feed parkinson_volatility calculation.
        """
        nifty_token = 256265
        cache_dir = settings.base_dir / "data" / "cache" / "intraday"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"NIFTY_5m_{as_of.strftime('%Y%m%d')}.csv"

        df = HistoricalDataLoader.fetch_real_data(
            kite_client=self.kite_client,
            instrument_token=nifty_token,
            start_date=as_of,
            end_date=as_of,
            interval="5minute",
            cache_path=cache_file,
            force_refresh=force_refresh,
        )
        return df

    def fetch_india_vix_series(
        self,
        start_date: date,
        end_date: date,
        force_refresh: bool = False,
    ) -> pd.Series:
        """
        Fetches daily closing levels of India VIX over [start_date, end_date].
        Returns pd.Series indexed by pd.DatetimeIndex.
        """
        vix_token = instrument_resolver.resolve_token(
            symbol="INDIA VIX",
            exchange="NSE",
            kite_client=self.kite_client,
        ) or 264969

        cache_dir = settings.base_dir / "data" / "cache" / "daily"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "INDIA_VIX_day.csv"

        df = HistoricalDataLoader.fetch_real_data(
            kite_client=self.kite_client,
            instrument_token=vix_token,
            start_date=start_date,
            end_date=end_date,
            interval="day",
            cache_path=cache_file,
            force_refresh=force_refresh,
        )
        if df is None or df.empty:
            raise RuntimeError(f"Could not load India VIX daily data for {start_date} -> {end_date}.")

        df["datetime"] = pd.to_datetime(df["datetime"])
        dates = pd.DatetimeIndex(df["datetime"].dt.normalize())
        return pd.Series(df["close"].to_numpy(dtype=float), index=dates, name="india_vix")
