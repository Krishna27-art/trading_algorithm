"""
300-Stock Universe Scanner & Explainable Stock Ranker.

Pulls live market quotes for all 300 universe constituents (100 Large, 100 Mid, 100 Small)
in batched requests via Kite Connect (up to 400 instruments per quote call).
Applies a separate Liquidity Filter layer, computes explainable ranking signals
(Gap %, RVOL, ATR Volatility, VWAP Distance), and combines them via transparent weighting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config.settings import InstrumentConfig, settings
from config.universe import (
    NIFTY_50_CONSTITUENTS,
    StockRecord,
    StockUniverse,
    create_instrument_config_for_equity,
    resolve_300_universe_tokens,
    resolve_universe_tokens,
)
from data.historical_loader import HistoricalDataLoader
from monitoring.logger import logger
from scanner.liquidity_filter import LiquidityFilter, LiquidityFilterResult, LiquidityStatus


@dataclass
class StockRankingMetrics:
    symbol: str
    token: Optional[int] = None
    category: str = "large"  # "large", "mid", "small"
    name: str = ""
    ltp: float = 0.0
    prev_close: float = 0.0
    open_price: float = 0.0
    gap_pct: float = 0.0
    volume: int = 0
    avg_volume_20d: int = 0
    rvol: float = 0.0
    atr_14: float = 0.0
    atr_pct: float = 0.0
    vwap: float = 0.0
    vwap_dist_pct: float = 0.0
    rvol_score: float = 0.0
    gap_score: float = 0.0
    vol_score: float = 0.0
    vwap_score: float = 0.0
    total_score: float = 0.0
    direction_bias: str = "NEUTRAL"
    liquidity_status: str = "PASS"
    rejection_reasons: List[str] = field(default_factory=list)
    rank: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class StockUniverseScanner:
    """
    Scans and ranks the 300-stock scanning universe (100 Large, 100 Mid, 100 Small).
    Supports batched Kite quotes and synthetic fallback when offline/testing.
    """

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or (Path(__file__).resolve().parent.parent / "data" / "cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.universe = StockUniverse()
        self.token_map = resolve_300_universe_tokens(cache_path=self.cache_dir / "universe_300_tokens.json")
        self.universe.print_startup_summary(self.token_map)
        self.liquidity_filter = LiquidityFilter()
        self.last_pipeline_summary: Dict[str, Any] = {}

    @staticmethod
    def calculate_atr_from_candles(candles_df: pd.DataFrame, period: int = 14) -> float:
        if len(candles_df) < 2:
            return 0.0
        df = candles_df.copy()
        df["prev_close"] = df["close"].shift(1)
        tr1 = df["high"] - df["low"]
        tr2 = (df["high"] - df["prev_close"]).abs()
        tr3 = (df["low"] - df["prev_close"]).abs()
        df["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr_series = df["tr"].rolling(window=min(period, len(df)), min_periods=1).mean()
        return float(atr_series.iloc[-1])

    @staticmethod
    def calculate_explainable_score(
        gap_pct: float,
        rvol: float,
        atr_pct: float,
        vwap_dist_pct: float,
    ) -> Tuple[float, float, float, float, float, str]:
        rvol_score = round(min(max(rvol, 0.0) / 2.0, 1.0) * 30.0, 2)
        gap_score = round(min(abs(gap_pct) / 2.5, 1.0) * 25.0, 2)
        vol_score = round(min(max(atr_pct, 0.0) / 2.5, 1.0) * 25.0, 2)
        vwap_score = round(min(abs(vwap_dist_pct) / 1.5, 1.0) * 20.0, 2)

        total_score = round(rvol_score + gap_score + vol_score + vwap_score, 2)

        if vwap_dist_pct > 0.15 and gap_pct > 0:
            bias = "LONG"
        elif vwap_dist_pct < -0.15 and gap_pct < 0:
            bias = "SHORT"
        elif vwap_dist_pct > 0:
            bias = "LONG"
        elif vwap_dist_pct < 0:
            bias = "SHORT"
        else:
            bias = "NEUTRAL"

        return rvol_score, gap_score, vol_score, vwap_score, total_score, bias

    def scan_universe(
        self,
        kite_client: Optional[Any] = None,
        top_n: int = 5,
        force_refresh_history: bool = False,
        allow_synthetic: bool = True,
    ) -> Tuple[List[StockRankingMetrics], str]:
        is_live_connected = False
        client = None

        if kite_client is not None:
            client = getattr(kite_client, "kite", kite_client)
            try:
                profile = client.profile()
                if profile and "user_id" in profile:
                    is_live_connected = True
            except Exception:
                is_live_connected = False

        if is_live_connected and client is not None:
            try:
                metrics = self._scan_real_kite(client, force_refresh_history)
                return self._rank_and_truncate(metrics, top_n), "REAL"
            except Exception as e:
                logger.error(f"Real Kite 300-stock scan failed: {e}")
                if not allow_synthetic:
                    raise RuntimeError(f"Real Kite market quote scan failed: {e}")

        if not allow_synthetic:
            raise RuntimeError(
                "Zerodha Kite Connect session is not authenticated or live quotes failed. "
                "Live market scanning requires an active Kite Connect session."
            )

        metrics = self._scan_synthetic(seed=42)
        return self._rank_and_truncate(metrics, top_n), "SYNTHETIC"

    def _scan_real_kite(
        self,
        kite: Any,
        force_refresh_history: bool = False,
    ) -> List[StockRankingMetrics]:
        all_records = self.universe.all_stocks
        symbols = [r.symbol for r in all_records]

        # Batched REST quotes (up to 150 per chunk to avoid payload/URL limits)
        quotes: Dict[str, Any] = {}
        batch_size = 150
        for i in range(0, len(symbols), batch_size):
            chunk = symbols[i : i + batch_size]
            quote_instruments = [f"NSE:{sym}" for sym in chunk]
            try:
                logger.info(f"Fetching batched live quotes for {len(quote_instruments)} instruments...")
                chunk_quotes = kite.quote(quote_instruments)
                if chunk_quotes:
                    quotes.update(chunk_quotes)
            except Exception as e:
                logger.warning(f"Error fetching quote batch [{i}:{i+batch_size}]: {e}")

        today = datetime.now().date()
        history_start = today - timedelta(days=40)
        results: List[StockRankingMetrics] = []

        tradable_cnt = 0
        setup_cnt = 0
        strong_cnt = 0

        for record in all_records:
            sym = record.symbol
            q_key = f"NSE:{sym}"
            q_data = quotes.get(q_key)

            if not q_data:
                results.append(
                    StockRankingMetrics(
                        symbol=sym,
                        name=record.name,
                        category=record.category,
                        token=self.token_map.get(sym),
                        liquidity_status=LiquidityStatus.DATA_UNAVAILABLE.value,
                        rejection_reasons=["No market quote received from Kite feed"],
                    )
                )
                continue

            token = self.token_map.get(sym) or q_data.get("instrument_token")
            ltp = float(q_data.get("last_price", 0.0))
            ohlc = q_data.get("ohlc", {})
            open_p = float(ohlc.get("open", ltp))
            prev_close = float(ohlc.get("close", ltp))
            volume = int(q_data.get("volume", 0))
            vwap = float(q_data.get("average_price", ltp)) or ltp

            # Fetch 20-day historical context safely
            try:
                avg_vol_20d, atr_14 = self._get_historical_context(
                    kite_client=kite,
                    symbol=sym,
                    token=token,
                    start_date=history_start,
                    end_date=today - timedelta(days=1),
                    force_refresh=force_refresh_history,
                )
            except Exception as e:
                logger.warning(f"Could not load history context for {sym}: {e}")
                avg_vol_20d, atr_14 = volume, round(ltp * 0.02, 2)

            # Evaluate Liquidity Filter
            raw_eval_dict = {
                "symbol": sym,
                "ltp": ltp,
                "volume": volume,
                "avg_volume_20d": avg_vol_20d,
                "depth": q_data.get("depth"),
                "upper_circuit_limit": q_data.get("upper_circuit_limit"),
                "lower_circuit_limit": q_data.get("lower_circuit_limit"),
            }

            liq_res = self.liquidity_filter.evaluate_stock(raw_eval_dict)

            if not liq_res.is_tradable:
                results.append(
                    StockRankingMetrics(
                        symbol=sym,
                        name=record.name,
                        token=token,
                        category=record.category,
                        ltp=ltp,
                        prev_close=prev_close,
                        open_price=open_p,
                        volume=volume,
                        avg_volume_20d=avg_vol_20d,
                        liquidity_status=liq_res.status.value,
                        rejection_reasons=liq_res.rejection_reasons,
                    )
                )
                continue

            tradable_cnt += 1
            setup_cnt += 1

            # Compute explainable signals
            gap_pct = round(((open_p - prev_close) / prev_close) * 100.0, 2) if prev_close > 0 else 0.0
            rvol = round(volume / avg_vol_20d, 2) if avg_vol_20d > 0 else 1.0
            atr_pct = round((atr_14 / prev_close) * 100.0, 2) if prev_close > 0 else 0.0
            vwap_dist_pct = round(((ltp - vwap) / vwap) * 100.0, 2) if vwap > 0 else 0.0

            rvol_sc, gap_sc, vol_sc, vwap_sc, tot_sc, bias = self.calculate_explainable_score(
                gap_pct=gap_pct,
                rvol=rvol,
                atr_pct=atr_pct,
                vwap_dist_pct=vwap_dist_pct,
            )

            if tot_sc >= 60.0:
                strong_cnt += 1

            results.append(
                StockRankingMetrics(
                    symbol=sym,
                    name=record.name,
                    token=token,
                    category=record.category,
                    ltp=ltp,
                    prev_close=prev_close,
                    open_price=open_p,
                    gap_pct=gap_pct,
                    volume=volume,
                    avg_volume_20d=avg_vol_20d,
                    rvol=rvol,
                    atr_14=round(atr_14, 2),
                    atr_pct=atr_pct,
                    vwap=round(vwap, 2),
                    vwap_dist_pct=vwap_dist_pct,
                    rvol_score=rvol_sc,
                    gap_score=gap_sc,
                    vol_score=vol_sc,
                    vwap_score=vwap_sc,
                    total_score=tot_sc,
                    direction_bias=bias,
                    liquidity_status=LiquidityStatus.PASS.value,
                )
            )

        self.last_pipeline_summary = {
            "universe_count": len(all_records),
            "tradable_count": tradable_cnt,
            "setup_count": setup_cnt,
            "strong_signal_count": strong_cnt,
        }

        return results

    def _get_historical_context(
        self,
        kite_client: Any,
        symbol: str,
        token: Optional[int],
        start_date: date,
        end_date: date,
        force_refresh: bool = False,
    ) -> Tuple[int, float]:
        if token is None:
            return 1000000, 25.0

        cache_path = self.cache_dir / f"{symbol}_daily_context.csv"

        try:
            class SupportsCandlesWrapper:
                def __init__(self, raw_kite: Any):
                    self.raw = raw_kite

                def get_historical_candles(
                    self,
                    instrument_token: int,
                    from_date: str,
                    to_date: str,
                    interval: str = "day",
                    continuous: bool = False,
                    oi: bool = False,
                ) -> List[dict]:
                    return self.raw.historical_data(
                        instrument_token=instrument_token,
                        from_date=from_date,
                        to_date=to_date,
                        interval=interval,
                        continuous=continuous,
                        oi=oi,
                    )

            wrapper = SupportsCandlesWrapper(kite_client)
            df = HistoricalDataLoader.fetch_real_data(
                kite_client=wrapper,
                instrument_token=token,
                start_date=start_date,
                end_date=end_date,
                interval="day",
                cache_path=cache_path,
                force_refresh=force_refresh,
            )
            if len(df) >= 5:
                avg_vol = int(df["volume"].tail(20).mean())
                atr = self.calculate_atr_from_candles(df.tail(20), period=14)
                return max(avg_vol, 1000), max(atr, 1.0)
        except Exception as e:
            logger.warning(f"Could not load historical context for {symbol}: {e}")

        return 1000000, 25.0

    def _scan_synthetic(self, seed: int = 42) -> List[StockRankingMetrics]:
        rng = np.random.default_rng(seed)
        all_records = self.universe.all_stocks
        results: List[StockRankingMetrics] = []

        base_prices = {
            "RELIANCE": 2950.0, "TCS": 4200.0, "HDFCBANK": 1650.0, "BHARTIARTL": 1550.0,
            "ICICIBANK": 1250.0, "INFY": 1850.0, "MRF": 135000.0, "DIXON": 12000.0,
            "BAJAJ-AUTO": 9200.0, "MARUTI": 12500.0, "ULTRACEMCO": 11500.0, "TRENT": 7100.0,
        }

        tradable_cnt = 0
        setup_cnt = 0
        strong_cnt = 0

        for i, record in enumerate(all_records):
            sym = record.symbol
            token = self.token_map.get(sym, 100000 + i)

            if record.category == "large":
                base_p = base_prices.get(sym, round(float(rng.uniform(400, 5000)), 2))
            elif record.category == "mid":
                base_p = base_prices.get(sym, round(float(rng.uniform(150, 2500)), 2))
            else:
                base_p = base_prices.get(sym, round(float(rng.uniform(50, 1200)), 2))

            prev_close = round(base_p, 2)
            gap_pct = round(float(rng.normal(0.2, 1.2)), 2)
            open_p = round(prev_close * (1 + gap_pct / 100.0), 2)
            drift_pct = float(rng.normal(0.1, 1.0))
            ltp = round(open_p * (1 + drift_pct / 100.0), 2)

            vwap = round((open_p * 0.45) + (ltp * 0.55) + float(rng.normal(0, base_p * 0.002)), 2)
            vwap_dist_pct = round(((ltp - vwap) / vwap) * 100.0, 2)

            avg_vol_20d = int(rng.uniform(200000, 3500000))
            boost = 2.2 if i in [2, 18, 36, 41, 85, 120, 190, 240, 280] else 1.0
            rvol = round(float(abs(rng.normal(1.1, 0.4))) * boost, 2)
            volume = int(avg_vol_20d * rvol)

            atr_pct = round(float(rng.uniform(1.2, 3.5)), 2)
            atr_14 = round(prev_close * (atr_pct / 100.0), 2)

            # Evaluate liquidity filter
            liq_res = self.liquidity_filter.evaluate_stock({
                "symbol": sym,
                "ltp": ltp,
                "volume": volume,
                "avg_volume_20d": avg_vol_20d,
            })

            if not liq_res.is_tradable:
                results.append(
                    StockRankingMetrics(
                        symbol=sym,
                        name=record.name,
                        token=token,
                        category=record.category,
                        ltp=ltp,
                        prev_close=prev_close,
                        open_price=open_p,
                        volume=volume,
                        avg_volume_20d=avg_vol_20d,
                        liquidity_status=liq_res.status.value,
                        rejection_reasons=liq_res.rejection_reasons,
                    )
                )
                continue

            tradable_cnt += 1
            setup_cnt += 1

            rvol_sc, gap_sc, vol_sc, vwap_sc, tot_sc, bias = self.calculate_explainable_score(
                gap_pct=gap_pct,
                rvol=rvol,
                atr_pct=atr_pct,
                vwap_dist_pct=vwap_dist_pct,
            )

            if tot_sc >= 60.0:
                strong_cnt += 1

            results.append(
                StockRankingMetrics(
                    symbol=sym,
                    name=record.name,
                    token=token,
                    category=record.category,
                    ltp=ltp,
                    prev_close=prev_close,
                    open_price=open_p,
                    gap_pct=gap_pct,
                    volume=volume,
                    avg_volume_20d=avg_vol_20d,
                    rvol=rvol,
                    atr_14=atr_14,
                    atr_pct=atr_pct,
                    vwap=vwap,
                    vwap_dist_pct=vwap_dist_pct,
                    rvol_score=rvol_sc,
                    gap_score=gap_sc,
                    vol_score=vol_sc,
                    vwap_score=vwap_sc,
                    total_score=tot_sc,
                    direction_bias=bias,
                    liquidity_status=LiquidityStatus.PASS.value,
                )
            )

        self.last_pipeline_summary = {
            "universe_count": len(all_records),
            "tradable_count": tradable_cnt,
            "setup_count": setup_cnt,
            "strong_signal_count": strong_cnt,
        }

        return results

    def _rank_and_truncate(
        self,
        metrics: List[StockRankingMetrics],
        top_n: int,
    ) -> List[StockRankingMetrics]:
        tradables = [m for m in metrics if m.liquidity_status == "PASS"]
        untradables = [m for m in metrics if m.liquidity_status != "PASS"]

        tradables.sort(key=lambda m: m.total_score, reverse=True)
        untradables.sort(key=lambda m: m.total_score, reverse=True)

        combined = tradables + untradables
        for i, m in enumerate(combined, start=1):
            m.rank = i

        return combined[:top_n] if top_n > 0 else combined

    def get_top_instrument_configs(
        self,
        top_candidates: List[StockRankingMetrics],
    ) -> List[InstrumentConfig]:
        configs: List[InstrumentConfig] = []
        for cand in top_candidates:
            cfg = create_instrument_config_for_equity(
                symbol=cand.symbol,
                token=cand.token,
                current_price=cand.ltp,
                atr_14=cand.atr_14,
            )
            configs.append(cfg)
        return configs


# Alias NiftyUniverseScanner for full backward compatibility
NiftyUniverseScanner = StockUniverseScanner
