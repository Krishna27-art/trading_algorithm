"""
NIFTY 50 Universe Scanner & Explainable Stock Ranker.

Pulls live market quotes for all 50 NIFTY constituents in ONE batched request
via Kite Connect (up to 500 instruments per quote call). Computes deterministic,
explainable ranking signals (Gap %, RVOL, ATR Volatility, VWAP Distance)
and combines them via a transparent weighted formula without black-box models.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config.settings import InstrumentConfig, settings
from config.universe import NIFTY_50_CONSTITUENTS, create_instrument_config_for_equity, resolve_universe_tokens
from data.historical_loader import HistoricalDataLoader
from monitoring.logger import logger


@dataclass
class StockRankingMetrics:
    symbol: str
    token: Optional[int]
    ltp: float
    prev_close: float
    open_price: float
    gap_pct: float
    volume: int
    avg_volume_20d: int
    rvol: float
    atr_14: float
    atr_pct: float
    vwap: float
    vwap_dist_pct: float
    rvol_score: float
    gap_score: float
    vol_score: float
    vwap_score: float
    total_score: float
    direction_bias: str  # "LONG", "SHORT", or "NEUTRAL"
    rank: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class NiftyUniverseScanner:
    """
    Scans and ranks the NIFTY 50 universe.
    Supports both real Kite batched quotes and a self-contained synthetic
    generator when Kite credentials are unauthenticated or offline.
    """

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or (Path(__file__).resolve().parent.parent / "data" / "cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.token_map = resolve_universe_tokens(cache_path=self.cache_dir / "nifty50_tokens.json")

    @staticmethod
    def calculate_atr_from_candles(candles_df: pd.DataFrame, period: int = 14) -> float:
        """Computes average true range from daily OHLC candles."""
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
        """
        Transparent weighted scoring system (0 to 100 points):
        - RVOL (30 pts): High participation / institutional interest.
        - Gap % (25 pts): Overnight momentum catalyst.
        - ATR % (25 pts): Sufficient range expansion capacity.
        - VWAP Distance (20 pts): Directional breakout clarity away from mean.

        Returns: (rvol_score, gap_score, vol_score, vwap_score, total_score, direction_bias)
        """
        # 1. RVOL: RVOL >= 2.0 gets max score (30). RVOL of 1.0 gets 15 pts.
        rvol_score = round(min(max(rvol, 0.0) / 2.0, 1.0) * 30.0, 2)

        # 2. Gap %: Gap >= 2.5% gets max score (25).
        gap_score = round(min(abs(gap_pct) / 2.5, 1.0) * 25.0, 2)

        # 3. ATR %: ATR >= 2.5% of price gets max score (25).
        vol_score = round(min(max(atr_pct, 0.0) / 2.5, 1.0) * 25.0, 2)

        # 4. VWAP Distance: >= 1.5% from VWAP gets max score (20).
        vwap_score = round(min(abs(vwap_dist_pct) / 1.5, 1.0) * 20.0, 2)

        total_score = round(rvol_score + gap_score + vol_score + vwap_score, 2)

        # Directional Bias determination
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
        """
        Executes the universe scan.
        Returns (ranked_metrics_list, data_source_label).
        data_source_label will be 'REAL' or 'SYNTHETIC'.
        """
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
                logger.error(f"Real Kite scan failed: {e}")
                if not allow_synthetic:
                    raise RuntimeError(f"Real Kite market quote scan failed: {e}")

        if not allow_synthetic:
            raise RuntimeError(
                "Zerodha Kite Connect session is not authenticated or live quotes failed. "
                "Live market scanning requires an active Kite Connect session. "
                "Please run 'python auth.py' or log in via the dashboard."
            )

        # Fallback to synthetic universe data ONLY if explicitly allowed
        metrics = self._scan_synthetic(seed=42)
        return self._rank_and_truncate(metrics, top_n), "SYNTHETIC"

    def _scan_real_kite(
        self,
        kite: Any,
        force_refresh_history: bool = False,
    ) -> List[StockRankingMetrics]:
        """
        Pulls real live quotes in ONE single batch call and historical context.
        """
        symbols = NIFTY_50_CONSTITUENTS
        quote_instruments = [f"NSE:{sym}" for sym in symbols]

        logger.info(f"Fetching batched live quotes for {len(quote_instruments)} NIFTY instruments...")
        quotes = kite.quote(quote_instruments)

        today = datetime.now().date()
        history_start = today - timedelta(days=40)  # to cover 20 trading days

        results: List[StockRankingMetrics] = []

        for sym in symbols:
            q_key = f"NSE:{sym}"
            q_data = quotes.get(q_key)
            if not q_data:
                logger.warning(f"No quote data received for {q_key}")
                continue

            token = self.token_map.get(sym) or q_data.get("instrument_token")
            ltp = float(q_data.get("last_price", 0.0))
            ohlc = q_data.get("ohlc", {})
            open_p = float(ohlc.get("open", ltp))
            prev_close = float(ohlc.get("close", ltp))
            volume = int(q_data.get("volume", 0))
            vwap = float(q_data.get("average_price", ltp)) or ltp

            # Fetch 20-day historical context for avg volume and ATR
            avg_vol_20d, atr_14 = self._get_historical_context(
                kite_client=kite,
                symbol=sym,
                token=token,
                start_date=history_start,
                end_date=today - timedelta(days=1),
                force_refresh=force_refresh_history,
            )

            # Compute metrics
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

            results.append(
                StockRankingMetrics(
                    symbol=sym,
                    token=token,
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
                )
            )

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
        """
        Retrieves 20-day historical daily candles to calculate 20-day average volume
        and 14-period ATR. Employs CSV caching to avoid repeated API calls.
        """
        if token is None:
            return 1000000, 25.0

        cache_path = self.cache_dir / f"{symbol}_daily_context.csv"

        try:
            # Wrap kite_client into SupportsHistoricalCandles protocol if needed
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

        # Sensible defaults for large caps if historical API is unavailable
        return 1500000, 30.0

    def _scan_synthetic(self, seed: int = 42) -> List[StockRankingMetrics]:
        """
        Produces realistic synthetic market states across all 50 constituents
        for testing, simulation, and offline demonstration.
        """
        rng = np.random.default_rng(seed)
        symbols = NIFTY_50_CONSTITUENTS
        results: List[StockRankingMetrics] = []

        # Diverse base price ranges for large caps
        base_prices = {
            "MRF": 135000.0, "DIXON": 12000.0, "BAJAJ-AUTO": 9200.0, "MARUTI": 12500.0,
            "ULTRACEMCO": 11500.0, "EICHERMOT": 4800.0, "TRENT": 7100.0, "TCS": 4200.0,
            "HEROMOTOCO": 5100.0, "TITAN": 3400.0, "INFY": 1850.0, "RELIANCE": 2950.0,
            "HDFCBANK": 1650.0, "ICICIBANK": 1250.0, "SBIN": 820.0, "TATASTEEL": 150.0,
            "ITC": 490.0, "ONGC": 290.0, "NTPC": 390.0, "COALINDIA": 480.0,
        }

        for i, sym in enumerate(symbols):
            token = self.token_map.get(sym, 100000 + i)
            base_p = base_prices.get(sym, round(float(rng.uniform(350, 4500)), 2))
            prev_close = round(base_p, 2)

            # Generate realistic gap (-2.5% to +2.8%)
            gap_pct = round(float(rng.normal(0.2, 1.1)), 2)
            open_p = round(prev_close * (1 + gap_pct / 100.0), 2)

            # Intraday movement
            drift_pct = float(rng.normal(0.1, 0.9))
            ltp = round(open_p * (1 + drift_pct / 100.0), 2)

            # Session VWAP (between open and ltp)
            vwap = round((open_p * 0.45) + (ltp * 0.55) + float(rng.normal(0, base_p * 0.002)), 2)
            vwap_dist_pct = round(((ltp - vwap) / vwap) * 100.0, 2)

            # Volume & RVOL
            avg_vol_20d = int(rng.uniform(800000, 4500000))
            # Inject a few distinct high-volume candidates (e.g., top momentum picks)
            boost = 2.2 if i in [2, 18, 36, 41, 47] else 1.0
            rvol = round(float(abs(rng.normal(1.1, 0.4))) * boost, 2)
            volume = int(avg_vol_20d * rvol)

            # ATR: typically 1.2% to 3.2% of stock price
            atr_pct = round(float(rng.uniform(1.2, 3.2)), 2)
            atr_14 = round(prev_close * (atr_pct / 100.0), 2)

            rvol_sc, gap_sc, vol_sc, vwap_sc, tot_sc, bias = self.calculate_explainable_score(
                gap_pct=gap_pct,
                rvol=rvol,
                atr_pct=atr_pct,
                vwap_dist_pct=vwap_dist_pct,
            )

            results.append(
                StockRankingMetrics(
                    symbol=sym,
                    token=token,
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
                )
            )

        return results

    def _rank_and_truncate(
        self,
        metrics: List[StockRankingMetrics],
        top_n: int,
    ) -> List[StockRankingMetrics]:
        """Sorts descending by total_score and assigns rank."""
        metrics.sort(key=lambda m: m.total_score, reverse=True)
        for i, m in enumerate(metrics, start=1):
            m.rank = i
        return metrics[:top_n] if top_n > 0 else metrics

    def get_top_instrument_configs(
        self,
        top_candidates: List[StockRankingMetrics],
    ) -> List[InstrumentConfig]:
        """
        Converts the top ranked scanner picks into configured InstrumentConfig
        instances ready for the ORB+VWAP EventDrivenBacktester or ExecutionEngine.
        """
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
