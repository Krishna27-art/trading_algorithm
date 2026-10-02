"""
300-stock master scanner and explainable stock ranker.

Live path:
    StockUniverse
        -> InstrumentResolver
        -> Zerodha Kite quotes
        -> cached completed daily context
        -> LiquidityFilter
        -> explainable ranking metrics
        -> ranked candidates

The live scanner is read-only. It never places orders and never fabricates
market data on the REAL path.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config.settings import InstrumentConfig, settings
from config.universe import (
    StockUniverse,
    create_instrument_config_for_equity,
)
from data.historical_loader import HistoricalDataLoader
from data.instrument_resolver import instrument_resolver
from data.market_calendar import MarketCalendar
from data.time_utils import today_ist
from monitoring.logger import logger
from scanner.liquidity_filter import LiquidityFilter, LiquidityStatus


@dataclass
class StockRankingMetrics:
    symbol: str
    token: Optional[int] = None
    category: str = "large"
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
    Scans and ranks the master stock universe.

    REAL mode requires:
      1. authenticated Kite client
      2. resolved NSE instrument token
      3. live quote with valid price/volume/VWAP fields
      4. cached daily context containing the latest completed trading day

    Synthetic data exists only behind the explicit allow_synthetic=True flag
    for tests/development and is never used as a REAL fallback.
    """

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or (
            settings.base_dir / "data" / "cache"
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.universe = StockUniverse()
        self.liquidity_filter = LiquidityFilter()

        self.token_map: Dict[str, int] = {}
        self.unresolved_symbols: List[str] = []
        self.last_pipeline_summary: Dict[str, Any] = {}

    def resolve_tokens(
        self,
        kite_client: Optional[Any] = None,
        force_refresh: bool = False,
    ) -> Dict[str, int]:
        """
        Resolve all universe symbols through InstrumentResolver.

        No token is invented locally. Unresolved symbols remain unresolved.
        """
        symbols = [record.symbol for record in self.universe.all_stocks]

        self.token_map, self.unresolved_symbols = (
            instrument_resolver.resolve_universe(
                symbols=symbols,
                kite_client=kite_client,
                cache_path=(
                    self.cache_dir / "universe_300_tokens.json"
                ),
                force_refresh=force_refresh,
            )
        )

        if self.unresolved_symbols:
            logger.error(
                "Token resolution incomplete: "
                f"{len(self.unresolved_symbols)} symbols unresolved."
            )

        self.universe.print_startup_summary(self.token_map)
        return self.token_map

    @staticmethod
    def _safe_float(
        value: Any,
        default: float = 0.0,
    ) -> float:
        """Return a finite float or a safe default."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default

        return number if np.isfinite(number) else default

    @staticmethod
    def _safe_int(
        value: Any,
        default: int = 0,
    ) -> int:
        """Return an integer or a safe default."""
        try:
            if value is None:
                return default
            number = int(float(value))
        except (TypeError, ValueError, OverflowError):
            return default

        return number

    @staticmethod
    def calculate_atr_from_candles(
        candles_df: pd.DataFrame,
        period: int = 14,
    ) -> float:
        """
        Calculate simple ATR from OHLC candles.

        The latest available completed candle is used. No future rows are
        generated or inferred.
        """
        if candles_df is None or len(candles_df) < 2:
            return 0.0

        required = {"high", "low", "close"}
        if not required.issubset(candles_df.columns):
            return 0.0

        df = candles_df.copy()

        for column in ("high", "low", "close"):
            df[column] = pd.to_numeric(
                df[column],
                errors="coerce",
            )

        df = df.dropna(
            subset=["high", "low", "close"]
        ).sort_values(
            "datetime"
            if "datetime" in df.columns
            else df.index.name or "high"
        )

        if len(df) < 2:
            return 0.0

        previous_close = df["close"].shift(1)

        tr1 = df["high"] - df["low"]
        tr2 = (df["high"] - previous_close).abs()
        tr3 = (df["low"] - previous_close).abs()

        df["tr"] = pd.concat(
            [tr1, tr2, tr3],
            axis=1,
        ).max(axis=1)

        window = min(int(period), len(df))
        atr_series = df["tr"].rolling(
            window=window,
            min_periods=1,
        ).mean()

        atr = StockUniverseScanner._safe_float(atr_series.iloc[-1])
        return max(atr, 0.0)

    @staticmethod
    def calculate_explainable_score(
        gap_pct: float,
        rvol: float,
        atr_pct: float,
        vwap_dist_pct: float,
    ) -> Tuple[float, float, float, float, float, str]:
        """
        Convert scanner factors to the documented 30/25/25/20 weighting.

        Scores are capped at their factor maximums.
        """
        gap_pct = float(gap_pct)
        rvol = float(rvol)
        atr_pct = float(atr_pct)
        vwap_dist_pct = float(vwap_dist_pct)

        rvol_score = round(
            min(max(rvol, 0.0) / 2.0, 1.0) * 30.0,
            2,
        )
        gap_score = round(
            min(abs(gap_pct) / 2.5, 1.0) * 25.0,
            2,
        )
        vol_score = round(
            min(max(atr_pct, 0.0) / 2.5, 1.0) * 25.0,
            2,
        )
        vwap_score = round(
            min(abs(vwap_dist_pct) / 1.5, 1.0) * 20.0,
            2,
        )

        total_score = round(
            rvol_score
            + gap_score
            + vol_score
            + vwap_score,
            2,
        )

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

        return (
            rvol_score,
            gap_score,
            vol_score,
            vwap_score,
            total_score,
            bias,
        )

    def _unavailable_result(
        self,
        record: Any,
        *,
        token: Optional[int] = None,
        ltp: float = 0.0,
        prev_close: float = 0.0,
        open_price: float = 0.0,
        volume: int = 0,
        avg_volume_20d: int = 0,
        reasons: Optional[List[str]] = None,
    ) -> StockRankingMetrics:
        """Create one consistent DATA_UNAVAILABLE scanner result."""
        return StockRankingMetrics(
            symbol=record.symbol,
            name=record.name,
            category=record.category,
            token=token,
            ltp=ltp,
            prev_close=prev_close,
            open_price=open_price,
            volume=volume,
            avg_volume_20d=avg_volume_20d,
            liquidity_status=(
                LiquidityStatus.DATA_UNAVAILABLE.value
            ),
            rejection_reasons=reasons or [
                "Required live market data unavailable"
            ],
        )

    def scan_universe(
        self,
        kite_client: Optional[Any] = None,
        top_n: int = 5,
        force_refresh_history: bool = False,
        allow_synthetic: bool = False,
    ) -> Tuple[List[StockRankingMetrics], str]:
        """
        Scan the full universe.

        REAL mode never falls back to synthetic data unless the caller
        explicitly passes allow_synthetic=True.
        """
        self.resolve_tokens(
            kite_client=kite_client,
            force_refresh=False,
        )

        is_live_connected = False
        client = None

        if kite_client is not None:
            client = getattr(
                kite_client,
                "kite",
                kite_client,
            )

            try:
                profile = client.profile()
                is_live_connected = bool(
                    isinstance(profile, dict)
                    and profile.get("user_id")
                )
            except Exception as exc:
                logger.warning(
                    f"Kite profile check failed: {exc}"
                )
                is_live_connected = False

        if is_live_connected and client is not None:
            if force_refresh_history:
                from scanner.history_context_warmer import (
                    daily_history_context_warmer,
                )

                latest_context_date = (
                    self._latest_completed_trading_day(
                        today_ist()
                    )
                )

                threading.Thread(
                    target=daily_history_context_warmer.refresh,
                    args=(
                        client,
                        latest_context_date,
                    ),
                    name="manual-daily-history-refresh",
                    daemon=True,
                ).start()

            try:
                metrics = self._scan_real_kite(
                    client,
                    force_refresh_history,
                )

                return (
                    self._rank_and_truncate(
                        metrics,
                        top_n,
                    ),
                    "REAL",
                )
            except Exception as exc:
                logger.error(
                    f"Real Kite 300-stock scan failed: {exc}"
                )

                if not allow_synthetic:
                    raise RuntimeError(
                        f"Real Kite market quote scan failed: {exc}"
                    ) from exc

        if not allow_synthetic:
            raise RuntimeError(
                "Zerodha Kite Connect session is not authenticated "
                "or live quotes failed. Live market scanning requires "
                "an active Kite Connect session."
            )

        metrics = self._scan_synthetic(seed=42)
        return (
            self._rank_and_truncate(
                metrics,
                top_n,
            ),
            "SYNTHETIC",
        )

    def _load_cached_historical_context(
        self,
        symbol: str,
        target_date: date,
    ) -> Optional[Tuple[int, float]]:
        """
        Read scanner daily context from local cache only.

        This method MUST NOT call Kite.

        Required context:
            * at least 20 daily bars
            * cache includes target_date
            * positive average volume
            * positive ATR
        """
        cache_path = (
            self.cache_dir
            / f"{symbol}_daily_context.csv"
        )

        if not cache_path.exists():
            return None

        try:
            df, _ = (
                HistoricalDataLoader
                .load_cached_data_with_validation(
                    cache_path
                )
            )

            if df is None or df.empty:
                return None

            if "datetime" not in df.columns:
                return None

            required = {
                "datetime",
                "high",
                "low",
                "close",
                "volume",
            }
            if not required.issubset(df.columns):
                return None

            df["datetime"] = pd.to_datetime(
                df["datetime"],
                errors="coerce",
            )

            df["volume"] = pd.to_numeric(
                df["volume"],
                errors="coerce",
            )

            df = df.dropna(
                subset=[
                    "datetime",
                    "high",
                    "low",
                    "close",
                    "volume",
                ]
            ).copy()

            if df.empty:
                return None

            df = df[
                df["datetime"].dt.date <= target_date
            ].copy()

            if len(df) < 20:
                return None

            latest_date = (
                df["datetime"]
                .dt.date
                .max()
            )

            if latest_date < target_date:
                return None

            recent_20 = (
                df.sort_values("datetime")
                .tail(20)
                .copy()
            )

            avg_vol = self._safe_float(
                recent_20["volume"].mean()
            )
            atr = self.calculate_atr_from_candles(
                recent_20,
                period=14,
            )

            if avg_vol <= 0 or atr <= 0:
                return None

            return (
                int(round(avg_vol)),
                max(atr, 1.0),
            )

        except Exception as exc:
            logger.warning(
                f"Invalid daily context cache for "
                f"{symbol}: {exc}"
            )
            return None

    @staticmethod
    def _latest_completed_trading_day(
        today: date,
    ) -> date:
        """
        Return the latest NSE trading day strictly before `today`.

        Scanner history baseline is previous completed trading day, not the
        still-forming current session.
        """
        candidate = today - timedelta(days=1)

        while not MarketCalendar.is_trading_day(
            candidate
        ):
            candidate -= timedelta(days=1)

        return candidate

    def _scan_real_kite(
        self,
        kite: Any,
        force_refresh_history: bool = False,
    ) -> List[StockRankingMetrics]:
        """
        Scan live Kite quotes and rank only stocks with valid REAL inputs.
        """
        all_records = self.universe.all_stocks
        symbols = [
            record.symbol
            for record in all_records
        ]

        quotes: Dict[str, Any] = {}
        batch_size = 150

        for i in range(
            0,
            len(symbols),
            batch_size,
        ):
            chunk = symbols[
                i : i + batch_size
            ]
            quote_instruments = [
                f"NSE:{symbol}"
                for symbol in chunk
            ]

            try:
                logger.info(
                    "Fetching batched live quotes for "
                    f"{len(quote_instruments)} instruments..."
                )

                chunk_quotes = kite.quote(
                    quote_instruments
                )

                if isinstance(chunk_quotes, dict):
                    quotes.update(chunk_quotes)
                else:
                    logger.warning(
                        "Kite returned a non-dict quote payload "
                        f"for batch [{i}:{i + batch_size}]."
                    )

            except Exception as exc:
                logger.warning(
                    "Error fetching quote batch "
                    f"[{i}:{i + batch_size}]: {exc}"
                )

        today = today_ist()
        latest_context_date = (
            self._latest_completed_trading_day(
                today
            )
        )

        results: List[StockRankingMetrics] = []

        tradable_cnt = 0
        setup_cnt = 0
        strong_cnt = 0

        for record in all_records:
            symbol = record.symbol
            quote_key = f"NSE:{symbol}"
            q_data = quotes.get(quote_key)

            if not isinstance(q_data, dict):
                results.append(
                    self._unavailable_result(
                        record,
                        token=self.token_map.get(symbol),
                        reasons=[
                            "No market quote received from Kite feed"
                        ],
                    )
                )
                continue

            token_raw = self.token_map.get(symbol)
            token = self._safe_int(
                token_raw,
                default=0,
            )

            if token <= 0:
                # IMPORTANT:
                # Do not reference ltp/open/close/volume here. These values
                # are not parsed until after token validation.
                results.append(
                    self._unavailable_result(
                        record,
                        token=None,
                        reasons=[
                            "No validated NSE instrument token available"
                        ],
                    )
                )
                continue

            ohlc = q_data.get("ohlc")
            if not isinstance(ohlc, dict):
                results.append(
                    self._unavailable_result(
                        record,
                        token=token,
                        reasons=[
                            "Kite quote missing OHLC payload"
                        ],
                    )
                )
                continue

            ltp = self._safe_float(
                q_data.get("last_price")
            )
            open_price = self._safe_float(
                ohlc.get("open")
            )
            prev_close = self._safe_float(
                ohlc.get("close")
            )
            volume = self._safe_int(
                q_data.get("volume")
            )
            vwap = self._safe_float(
                q_data.get("average_price")
            )

            invalid_quote_fields: List[str] = []

            if ltp <= 0:
                invalid_quote_fields.append(
                    "invalid LTP"
                )

            if open_price <= 0:
                invalid_quote_fields.append(
                    "invalid session open"
                )

            if prev_close <= 0:
                invalid_quote_fields.append(
                    "invalid previous close"
                )

            if volume <= 0:
                invalid_quote_fields.append(
                    "invalid/zero session volume"
                )

            if vwap <= 0:
                invalid_quote_fields.append(
                    "invalid/zero session VWAP"
                )

            if invalid_quote_fields:
                results.append(
                    self._unavailable_result(
                        record,
                        token=token,
                        ltp=ltp,
                        prev_close=prev_close,
                        open_price=open_price,
                        volume=volume,
                        reasons=[
                            "Kite quote failed validation: "
                            + ", ".join(
                                invalid_quote_fields
                            )
                        ],
                    )
                )
                continue

            cached_context = (
                self._load_cached_historical_context(
                    symbol=symbol,
                    target_date=latest_context_date,
                )
            )

            if cached_context is None:
                results.append(
                    self._unavailable_result(
                        record,
                        token=token,
                        ltp=ltp,
                        prev_close=prev_close,
                        open_price=open_price,
                        volume=volume,
                        reasons=[
                            "Daily historical context cache "
                            "not warmed for latest completed "
                            "trading day"
                        ],
                    )
                )
                continue

            avg_vol_20d, atr_14 = cached_context

            raw_eval_dict = {
                "symbol": symbol,
                "ltp": ltp,
                "volume": volume,
                "avg_volume_20d": avg_vol_20d,
                "depth": q_data.get("depth"),
                "upper_circuit_limit": q_data.get(
                    "upper_circuit_limit"
                ),
                "lower_circuit_limit": q_data.get(
                    "lower_circuit_limit"
                ),
            }

            try:
                liq_res = (
                    self.liquidity_filter.evaluate_stock(
                        raw_eval_dict
                    )
                )
            except Exception as exc:
                logger.error(
                    f"Liquidity evaluation failed for "
                    f"{symbol}: {exc}"
                )

                results.append(
                    self._unavailable_result(
                        record,
                        token=token,
                        ltp=ltp,
                        prev_close=prev_close,
                        open_price=open_price,
                        volume=volume,
                        avg_volume_20d=avg_vol_20d,
                        reasons=[
                            "Liquidity filter evaluation failed"
                        ],
                    )
                )
                continue

            if not liq_res.is_tradable:
                results.append(
                    StockRankingMetrics(
                        symbol=symbol,
                        name=record.name,
                        category=record.category,
                        token=token,
                        ltp=ltp,
                        prev_close=prev_close,
                        open_price=open_price,
                        volume=volume,
                        avg_volume_20d=avg_vol_20d,
                        liquidity_status=liq_res.status.value,
                        rejection_reasons=list(
                            liq_res.rejection_reasons
                        ),
                    )
                )
                continue

            tradable_cnt += 1
            setup_cnt += 1

            gap_pct = round(
                (
                    (open_price - prev_close)
                    / prev_close
                )
                * 100.0,
                2,
            )

            rvol = round(
                volume / avg_vol_20d,
                2,
            )

            if atr_14 <= 0:
                results.append(
                    self._unavailable_result(
                        record,
                        token=token,
                        ltp=ltp,
                        prev_close=prev_close,
                        open_price=open_price,
                        volume=volume,
                        avg_volume_20d=avg_vol_20d,
                        reasons=[
                            "Historical ATR-14 unavailable"
                        ],
                    )
                )
                continue

            atr_pct = round(
                (atr_14 / prev_close) * 100.0,
                2,
            )

            vwap_dist_pct = round(
                (
                    (ltp - vwap)
                    / vwap
                )
                * 100.0,
                2,
            )

            (
                rvol_score,
                gap_score,
                vol_score,
                vwap_score,
                total_score,
                bias,
            ) = self.calculate_explainable_score(
                gap_pct=gap_pct,
                rvol=rvol,
                atr_pct=atr_pct,
                vwap_dist_pct=vwap_dist_pct,
            )

            if total_score >= 60.0:
                strong_cnt += 1

            results.append(
                StockRankingMetrics(
                    symbol=symbol,
                    name=record.name,
                    token=token,
                    category=record.category,
                    ltp=ltp,
                    prev_close=prev_close,
                    open_price=open_price,
                    gap_pct=gap_pct,
                    volume=volume,
                    avg_volume_20d=avg_vol_20d,
                    rvol=rvol,
                    atr_14=round(atr_14, 2),
                    atr_pct=atr_pct,
                    vwap=round(vwap, 2),
                    vwap_dist_pct=vwap_dist_pct,
                    rvol_score=rvol_score,
                    gap_score=gap_score,
                    vol_score=vol_score,
                    vwap_score=vwap_score,
                    total_score=total_score,
                    direction_bias=bias,
                    liquidity_status=(
                        LiquidityStatus.PASS.value
                    ),
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
        """
        Legacy historical-context helper.

        The main live scanner deliberately uses
        `_load_cached_historical_context()` so ranking a universe scan does
        not issue 300 individual historical API requests.
        """
        if token is None:
            logger.warning(
                f"No numerical instrument_token found for {symbol}. "
                "Historical context unavailable; stock will not be ranked."
            )
            return 0, 0.0

        cache_path = (
            self.cache_dir
            / f"{symbol}_daily_context.csv"
        )

        try:
            df = HistoricalDataLoader.fetch_real_data(
                kite_client=kite_client,
                instrument_token=token,
                start_date=start_date,
                end_date=end_date,
                interval="day",
                cache_path=cache_path,
                force_refresh=force_refresh,
            )

            if df is not None and len(df) >= 20:
                recent_20 = (
                    df.sort_values("datetime")
                    .tail(20)
                )

                avg_vol = self._safe_float(
                    recent_20["volume"].mean()
                )
                atr = self.calculate_atr_from_candles(
                    recent_20,
                    period=14,
                )

                if avg_vol <= 0:
                    logger.warning(
                        f"Invalid 20-day average volume "
                        f"for {symbol}: {avg_vol}"
                    )
                    return 0, 0.0

                return (
                    int(round(avg_vol)),
                    max(atr, 1.0),
                )

            logger.warning(
                f"Insufficient full-session history "
                f"for {symbol}: "
                f"{len(df) if df is not None else 0} "
                "daily bars available, 20 required"
            )
        except Exception as exc:
            logger.warning(
                f"Could not load historical context "
                f"for {symbol}: {exc}"
            )

        return 0, 0.0

    def _scan_synthetic(
        self,
        seed: int = 42,
    ) -> List[StockRankingMetrics]:
        """
        Explicit synthetic-only path for tests/development.

        This function is never called by the REAL path unless the caller
        explicitly enables allow_synthetic=True.
        """
        rng = np.random.default_rng(seed)
        all_records = self.universe.all_stocks
        results: List[StockRankingMetrics] = []

        base_prices = {
            "RELIANCE": 2950.0,
            "TCS": 4200.0,
            "HDFCBANK": 1650.0,
            "BHARTIARTL": 1550.0,
            "ICICIBANK": 1250.0,
            "INFY": 1850.0,
            "MRF": 135000.0,
            "DIXON": 12000.0,
            "BAJAJ-AUTO": 9200.0,
            "MARUTI": 12500.0,
            "ULTRACEMCO": 11500.0,
            "TRENT": 7100.0,
        }

        tradable_cnt = 0
        setup_cnt = 0
        strong_cnt = 0

        for i, record in enumerate(all_records):
            symbol = record.symbol

            # Synthetic token is only metadata for the explicit synthetic test
            # path. It is never used by the REAL scanner.
            token = self.token_map.get(
                symbol,
                100000 + i,
            )

            if record.category == "large":
                base_price = base_prices.get(
                    symbol,
                    round(
                        float(
                            rng.uniform(
                                400,
                                5000,
                            )
                        ),
                        2,
                    ),
                )
            elif record.category == "mid":
                base_price = base_prices.get(
                    symbol,
                    round(
                        float(
                            rng.uniform(
                                150,
                                2500,
                            )
                        ),
                        2,
                    ),
                )
            else:
                base_price = base_prices.get(
                    symbol,
                    round(
                        float(
                            rng.uniform(
                                50,
                                1200,
                            )
                        ),
                        2,
                    ),
                )

            prev_close = round(
                base_price,
                2,
            )

            gap_pct = round(
                float(
                    rng.normal(
                        0.2,
                        1.2,
                    )
                ),
                2,
            )

            open_price = round(
                prev_close
                * (
                    1
                    + gap_pct / 100.0
                ),
                2,
            )

            drift_pct = float(
                rng.normal(
                    0.1,
                    1.0,
                )
            )

            ltp = round(
                open_price
                * (
                    1
                    + drift_pct / 100.0
                ),
                2,
            )

            vwap = round(
                (
                    open_price * 0.45
                )
                + (
                    ltp * 0.55
                )
                + float(
                    rng.normal(
                        0,
                        base_price * 0.002,
                    )
                ),
                2,
            )

            vwap_dist_pct = round(
                (
                    (ltp - vwap)
                    / vwap
                )
                * 100.0,
                2,
            )

            avg_vol_20d = int(
                rng.uniform(
                    200000,
                    3500000,
                )
            )

            boost = (
                2.2
                if i
                in [
                    2,
                    18,
                    36,
                    41,
                    85,
                    120,
                    190,
                    240,
                    280,
                ]
                else 1.0
            )

            rvol = round(
                float(
                    abs(
                        rng.normal(
                            1.1,
                            0.4,
                        )
                    )
                )
                * boost,
                2,
            )

            volume = int(
                avg_vol_20d
                * rvol
            )

            atr_pct = round(
                float(
                    rng.uniform(
                        1.2,
                        3.5,
                    )
                ),
                2,
            )

            atr_14 = round(
                prev_close
                * (
                    atr_pct / 100.0
                ),
                2,
            )

            liq_res = (
                self.liquidity_filter
                .evaluate_stock(
                    {
                        "symbol": symbol,
                        "ltp": ltp,
                        "volume": volume,
                        "avg_volume_20d": avg_vol_20d,
                    }
                )
            )

            if not liq_res.is_tradable:
                results.append(
                    StockRankingMetrics(
                        symbol=symbol,
                        name=record.name,
                        token=token,
                        category=record.category,
                        ltp=ltp,
                        prev_close=prev_close,
                        open_price=open_price,
                        volume=volume,
                        avg_volume_20d=avg_vol_20d,
                        liquidity_status=(
                            liq_res.status.value
                        ),
                        rejection_reasons=(
                            liq_res.rejection_reasons
                        ),
                    )
                )
                continue

            tradable_cnt += 1
            setup_cnt += 1

            (
                rvol_score,
                gap_score,
                vol_score,
                vwap_score,
                total_score,
                bias,
            ) = self.calculate_explainable_score(
                gap_pct=gap_pct,
                rvol=rvol,
                atr_pct=atr_pct,
                vwap_dist_pct=vwap_dist_pct,
            )

            if total_score >= 60.0:
                strong_cnt += 1

            results.append(
                StockRankingMetrics(
                    symbol=symbol,
                    name=record.name,
                    token=token,
                    category=record.category,
                    ltp=ltp,
                    prev_close=prev_close,
                    open_price=open_price,
                    gap_pct=gap_pct,
                    volume=volume,
                    avg_volume_20d=avg_vol_20d,
                    rvol=rvol,
                    atr_14=atr_14,
                    atr_pct=atr_pct,
                    vwap=vwap,
                    vwap_dist_pct=vwap_dist_pct,
                    rvol_score=rvol_score,
                    gap_score=gap_score,
                    vol_score=vol_score,
                    vwap_score=vwap_score,
                    total_score=total_score,
                    direction_bias=bias,
                    liquidity_status=(
                        LiquidityStatus.PASS.value
                    ),
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
        """
        Rank PASS stocks first, then rejected/unavailable stocks.

        This preserves diagnostics while ensuring a DATA_UNAVAILABLE stock
        can never outrank a usable PASS candidate.
        """
        if top_n < 0:
            raise ValueError(
                "top_n must be >= 0"
            )

        tradables = [
            metric
            for metric in metrics
            if metric.liquidity_status
            == LiquidityStatus.PASS.value
        ]

        untradables = [
            metric
            for metric in metrics
            if metric.liquidity_status
            != LiquidityStatus.PASS.value
        ]

        tradables.sort(
            key=lambda metric: (
                -metric.total_score,
                metric.symbol,
            )
        )

        untradables.sort(
            key=lambda metric: (
                -metric.total_score,
                metric.symbol,
            )
        )

        combined = tradables + untradables

        for rank, metric in enumerate(
            combined,
            start=1,
        ):
            metric.rank = rank

        return (
            combined[:top_n]
            if top_n > 0
            else combined
        )

    def get_top_instrument_configs(
        self,
        top_candidates: List[StockRankingMetrics],
    ) -> List[InstrumentConfig]:
        """
        Build equity InstrumentConfig objects using the actual candidate LTP
        and actual resolved Kite token.
        """
        configs: List[InstrumentConfig] = []

        for candidate in top_candidates:
            cfg = create_instrument_config_for_equity(
                symbol=candidate.symbol,
                token=candidate.token,
                current_price=candidate.ltp,
                atr_14=candidate.atr_14,
            )
            configs.append(cfg)

        return configs


# Alias retained for backward compatibility with existing imports.
NiftyUniverseScanner = StockUniverseScanner
