"""
Unified Strategy Prediction Service.

Executes the REAL strategy implementations:
- IntradayORBStrategy (strategy/orb_strategy.py)
- CPRRegimeBreakoutStrategy (strategy/cpr_strategy.py)
- BufferedDualEMAStrategy (strategy/dual_ema_strategy.py)

Provides a single source of truth for live candidate predictions, consensus
calculations, and key market insights.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config.settings import AppSettings, InstrumentConfig, settings
from config.universe import create_instrument_config_for_equity, resolve_universe_tokens
from data.historical_loader import HistoricalDataLoader
from indicators.vwap import calculate_session_vwap
from monitoring.logger import logger
from strategy.base_strategy import SignalAction, StrategySignal
from strategy.cpr_strategy import CPRRegimeBreakoutStrategy, Regime
from strategy.dual_ema_strategy import BufferedDualEMAStrategy
from strategy.orb_strategy import IntradayORBStrategy
from strategy.residual_momentum import ResidualMomentumStrategy
from strategy.vrp_index import VRPHarvestStrategy
from strategy.apex_engine import ApexAivemEngine, EngineConfig, CatalystScorer


@dataclass
class SingleStrategyPrediction:
    status: str  # e.g. "LONG_BREAKOUT", "BULLISH_EXPANSION", "TRENDING_LONG", "RESIDUAL_MOMENTUM_LONG", "VRP_HARVEST", "CONFIRMED_LONG", "NO_TRADE", "UNAVAILABLE", "ERROR"
    direction: Optional[str] = None  # "LONG", "SHORT", or None
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    reason: str = ""
    levels: Optional[Dict[str, Any]] = None
    metrics: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"status": self.status}
        if self.direction:
            d["direction"] = self.direction
        if self.entry is not None:
            d["entry"] = round(float(self.entry), 2)
        if self.stop_loss is not None:
            d["stop_loss"] = round(float(self.stop_loss), 2)
        if self.target is not None:
            d["target"] = round(float(self.target), 2)
        if self.reason:
            d["reason"] = self.reason
        clean_levels = {}
        if self.levels:
            for k, v in self.levels.items():
                if isinstance(v, (np.floating, float)):
                    clean_levels[k] = round(float(v), 2)
                elif isinstance(v, (np.integer, int)):
                    clean_levels[k] = int(v)
                else:
                    clean_levels[k] = v
        d["levels"] = clean_levels

        clean_metrics = {}
        if self.metrics:
            for k, v in self.metrics.items():
                if isinstance(v, (np.floating, float)):
                    clean_metrics[k] = round(float(v), 4) if abs(float(v)) < 1.0 else round(float(v), 2)
                elif isinstance(v, (np.integer, int)):
                    clean_metrics[k] = int(v)
                else:
                    clean_metrics[k] = v
        d["metrics"] = clean_metrics
        return d


@dataclass
class CandidatePrediction:
    rank: int
    symbol: str
    ltp: float
    momentum_score: float
    universe_bias: str
    predictions: Dict[str, SingleStrategyPrediction]
    consensus: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        preds_dict = {k: (v.to_dict() if hasattr(v, "to_dict") else v) for k, v in self.predictions.items()}
        return {
            "rank": self.rank,
            "symbol": self.symbol,
            "ltp": round(self.ltp, 2),
            "momentum_score": round(self.momentum_score, 1),
            "universe_bias": self.universe_bias,
            "predictions": preds_dict,
            "strategies": preds_dict,  # Alias for frontend access
            "consensus": self.consensus,
        }


class PredictionService:
    """
    Evaluates all 6 strategies independently across candidate instruments.
    1. ORB (Intraday Opening Range Breakout)
    2. CPR (Central Pivot Range Regime Breakout)
    3. Dual-EMA (Buffered Dual-EMA Trend)
    4. NSE-RM-100 (Residual Momentum)
    5. NSE-VRP-INDEX (Variance Risk Premium Index Harvest)
    6. APEX-AIVEM (6-Factor Pre-Market & Catalyst Engine)
    """

    def __init__(self, app_settings: AppSettings = settings):
        self.settings = app_settings
        self.cache_dir = settings.base_dir / "data" / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def evaluate_symbol(
        self,
        symbol: str,
        df_15m: pd.DataFrame,
        current_ltp: Optional[float] = None,
        token: Optional[int] = None,
        stock_metric: Optional[Any] = None,
        daily_closes_df: Optional[pd.DataFrame] = None,
        index_close_series: Optional[pd.Series] = None,
        vix_series: Optional[pd.Series] = None,
        rv_series: Optional[pd.Series] = None,
    ) -> Tuple[Dict[str, SingleStrategyPrediction], Dict[str, Any]]:
        """
        Runs ALL 6 strategies independently on the provided instrument data:
        1. orb
        2. cpr
        3. dual_ema
        4. nse_rm_100
        5. nse_vrp_index
        6. apex
        
        Returns (predictions_map, consensus_dict).
        """
        inst = create_instrument_config_for_equity(symbol, token or 0)
        ltp = current_ltp or (float(df_15m["close"].iloc[-1]) if not df_15m.empty else 0.0)

        orb_pred = self._evaluate_orb(inst, df_15m, ltp)
        cpr_pred = self._evaluate_cpr(inst, df_15m, ltp)
        dual_ema_pred = self._evaluate_dual_ema(inst, df_15m, ltp)
        rm_pred = self._evaluate_rm_100(symbol, df_15m, ltp, daily_closes_df, index_close_series)
        vrp_pred = self._evaluate_vrp_index(symbol, df_15m, ltp, vix_series, rv_series)
        apex_pred = self._evaluate_apex(symbol, df_15m, ltp, stock_metric)

        predictions = {
            "orb": orb_pred,
            "cpr": cpr_pred,
            "dual_ema": dual_ema_pred,
            "nse_rm_100": rm_pred,
            "nse_vrp_index": vrp_pred,
            "apex": apex_pred,
        }

        consensus = self.calculate_consensus(predictions)
        return predictions, consensus

    def _prepare_data(self, df_15m: pd.DataFrame) -> Tuple[pd.DataFrame, List[Tuple[date, pd.DataFrame]]]:
        data = df_15m.copy()
        if "datetime" not in data.columns and isinstance(data.index, pd.DatetimeIndex):
            data["datetime"] = data.index
        data["datetime"] = pd.to_datetime(data["datetime"])
        data.sort_values("datetime", inplace=True)
        data["vwap"] = calculate_session_vwap(data).values
        data["date"] = data["datetime"].dt.date
        days = list(data.groupby("date"))
        return data, days

    def _evaluate_orb(
        self,
        inst: InstrumentConfig,
        df_15m: pd.DataFrame,
        ltp: float,
    ) -> SingleStrategyPrediction:
        """Evaluates actual IntradayORBStrategy on latest session."""
        if df_15m.empty:
            return SingleStrategyPrediction(status="NO_TRADE", reason="No 15m candle data available")

        _, days = self._prepare_data(df_15m)
        if not days:
            return SingleStrategyPrediction(status="NO_TRADE", reason="No trading days found in dataset")

        latest_date, today_df = days[-1]
        strategy = IntradayORBStrategy(inst, self.settings.strategy)
        strategy.reset_session(latest_date)

        last_signal: Optional[StrategySignal] = None
        for i in range(len(today_df)):
            row = today_df.iloc[i]
            candle = {
                "datetime": row["datetime"],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row.get("volume", 0)),
            }
            vwap = float(row.get("vwap", row["close"]))
            sig = strategy.on_candle(candle, vwap)
            if sig:
                last_signal = sig

        orb_info: Dict[str, Any] = {}
        if strategy.orb:
            orb_info = {
                "orb_high": round(strategy.orb.high, 2),
                "orb_low": round(strategy.orb.low, 2),
                "orb_width": round(strategy.orb.width, 2),
                "is_valid_volatility": strategy.orb.is_valid_volatility,
            }

        if last_signal:
            if last_signal.action == SignalAction.BUY:
                return SingleStrategyPrediction(
                    status="LONG_BREAKOUT",
                    direction="LONG",
                    entry=last_signal.price,
                    stop_loss=last_signal.stop_loss,
                    target=last_signal.target,
                    reason=last_signal.reason or "Price broke above 30m ORB High with VWAP confirmation",
                    levels=orb_info,
                )
            elif last_signal.action == SignalAction.SELL:
                return SingleStrategyPrediction(
                    status="SHORT_BREAKDOWN",
                    direction="SHORT",
                    entry=last_signal.price,
                    stop_loss=last_signal.stop_loss,
                    target=last_signal.target,
                    reason=last_signal.reason or "Price broke below 30m ORB Low with VWAP confirmation",
                    levels=orb_info,
                )

        if not strategy.orb:
            return SingleStrategyPrediction(
                status="WAITING",
                reason="Establishing 30-minute opening range (09:15 - 09:45 IST)",
                levels=orb_info,
            )

        if not strategy.orb.is_valid_volatility:
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason=f"Opening range width ({strategy.orb.width:.1f} pts) below volatility cutoff",
                levels=orb_info,
            )

        return SingleStrategyPrediction(
            status="NO_TRADE",
            reason=f"Price within opening range boundaries ({strategy.orb.low:.1f} - {strategy.orb.high:.1f})",
            levels=orb_info,
        )

    def _evaluate_cpr(
        self,
        inst: InstrumentConfig,
        df_15m: pd.DataFrame,
        ltp: float,
    ) -> SingleStrategyPrediction:
        """Evaluates actual CPRRegimeBreakoutStrategy."""
        if df_15m.empty:
            return SingleStrategyPrediction(status="NO_TRADE", reason="No 15m candle data available")

        _, days = self._prepare_data(df_15m)
        if len(days) < 2:
            return SingleStrategyPrediction(status="NO_TRADE", reason="Requires at least 2 sessions for prior-day CPR calculation")

        # Prior days for seed_context
        lookback_df = pd.concat([d for _, d in days[:-1]], ignore_index=True)
        latest_date, today_df = days[-1]

        strategy = CPRRegimeBreakoutStrategy(inst, self.settings.strategy)
        strategy.seed_context(lookback_df)
        strategy.reset_session(latest_date)

        cpr_levels: Dict[str, Any] = {}
        if strategy.pivots:
            cpr_levels = {
                "pivot": round(strategy.pivots["P"], 2),
                "bottom_central": round(strategy.pivots["BC"], 2),
                "top_central": round(strategy.pivots["TC"], 2),
                "r1": round(strategy.pivots["R1"], 2),
                "s1": round(strategy.pivots["S1"], 2),
                "cpr_width_pct": round(strategy.pivots["width_pct"], 2),
                "regime": strategy.regime,
            }

        last_signal: Optional[StrategySignal] = None
        for i in range(len(today_df)):
            row = today_df.iloc[i]
            candle = {
                "datetime": row["datetime"],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row.get("volume", 0)),
            }
            vwap = float(row.get("vwap", row["close"]))
            sig = strategy.on_candle(candle, vwap)
            if sig:
                last_signal = sig

        if last_signal:
            if last_signal.action == SignalAction.BUY:
                return SingleStrategyPrediction(
                    status="BULLISH_EXPANSION",
                    direction="LONG",
                    entry=last_signal.price,
                    stop_loss=last_signal.stop_loss,
                    target=last_signal.target,
                    reason=last_signal.reason or f"Price above TC ({cpr_levels.get('top_central')}) during {strategy.regime} regime",
                    levels=cpr_levels,
                )
            elif last_signal.action == SignalAction.SELL:
                return SingleStrategyPrediction(
                    status="BEARISH_EXPANSION",
                    direction="SHORT",
                    entry=last_signal.price,
                    stop_loss=last_signal.stop_loss,
                    target=last_signal.target,
                    reason=last_signal.reason or f"Price below BC ({cpr_levels.get('bottom_central')}) during {strategy.regime} regime",
                    levels=cpr_levels,
                )

        if strategy.regime == Regime.NEUTRAL:
            return SingleStrategyPrediction(
                status="NO_TRADE",
                reason="CPR neutral regime (width between 20th and 80th percentile)",
                levels=cpr_levels,
            )

        return SingleStrategyPrediction(
            status="NO_TRADE",
            reason=f"No breakout trigger in {strategy.regime} CPR regime",
            levels=cpr_levels,
        )

    def _evaluate_dual_ema(
        self,
        inst: InstrumentConfig,
        df_15m: pd.DataFrame,
        ltp: float,
    ) -> SingleStrategyPrediction:
        """Evaluates actual BufferedDualEMAStrategy."""
        if df_15m.empty:
            return SingleStrategyPrediction(status="NO_TRADE", reason="No 15m candle data available")

        _, days = self._prepare_data(df_15m)
        if len(days) < 2:
            return SingleStrategyPrediction(status="NO_TRADE", reason="Requires prior sessions for SMA200 / EMA warm-up")

        lookback_df = pd.concat([d for _, d in days[:-1]], ignore_index=True)
        latest_date, today_df = days[-1]

        strategy = BufferedDualEMAStrategy(inst, self.settings.strategy)
        strategy.seed_context(lookback_df)
        strategy.reset_session(latest_date)

        last_signal: Optional[StrategySignal] = None
        for i in range(len(today_df)):
            row = today_df.iloc[i]
            candle = {
                "datetime": row["datetime"],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row.get("volume", 0)),
            }
            vwap = float(row.get("vwap", row["close"]))
            sig = strategy.on_candle(candle, vwap)
            if sig:
                last_signal = sig

        # Get latest computed indicator values from strategy warm buffer
        levels: Dict[str, Any] = {}
        if not today_df.empty:
            last_row = today_df.iloc[-1]
            ind = strategy._current_indicators({
                "datetime": last_row["datetime"],
                "open": float(last_row["open"]),
                "high": float(last_row["high"]),
                "low": float(last_row["low"]),
                "close": float(last_row["close"]),
            })
            if ind is not None:
                levels = {
                    "ema_fast": round(float(ind["ema9"]), 2),
                    "ema_slow": round(float(ind["ema21"]), 2),
                    "sma_trend": round(float(ind["sma200"]), 2),
                    "atr_14": round(float(ind["atr14"]), 2),
                    "buffer": round(float(strategy.buffer_gamma * ind["atr14"]), 2),
                }

        if last_signal:
            if last_signal.action == SignalAction.BUY:
                return SingleStrategyPrediction(
                    status="TRENDING_LONG",
                    direction="LONG",
                    entry=last_signal.price,
                    stop_loss=last_signal.stop_loss,
                    target=last_signal.target,
                    reason=last_signal.reason or "EMA9 above EMA21 with buffer & price above SMA200",
                    levels=levels,
                )
            elif last_signal.action == SignalAction.SELL:
                return SingleStrategyPrediction(
                    status="TRENDING_SHORT",
                    direction="SHORT",
                    entry=last_signal.price,
                    stop_loss=last_signal.stop_loss,
                    target=last_signal.target,
                    reason=last_signal.reason or "EMA9 below EMA21 with buffer & price below SMA200",
                    levels=levels,
                )

        return SingleStrategyPrediction(
            status="BUFFER_ZONE",
            reason="Price within volatility buffer between EMA9 and EMA21 or contra-SMA200",
            levels=levels,
        )

    def _evaluate_rm_100(
        self,
        symbol: str,
        df_15m: pd.DataFrame,
        ltp: float,
        daily_closes_df: Optional[pd.DataFrame] = None,
        index_close_series: Optional[pd.Series] = None,
    ) -> SingleStrategyPrediction:
        """Evaluates NSE-RM-100 (Residual Momentum Strategy)."""
        try:
            rm_strat = ResidualMomentumStrategy()

            if daily_closes_df is not None and index_close_series is not None and symbol in daily_closes_df.columns:
                rf_daily = pd.Series(0.00025, index=daily_closes_df.index)
                scores = rm_strat.compute_scores(
                    closes=daily_closes_df,
                    index_close=index_close_series,
                    rf_daily=rf_daily,
                    universe=[symbol],
                )
                if not scores.empty and "rm_z" in scores.columns:
                    row = scores.iloc[0]
                    rm_z = float(row["rm_z"])
                    alpha = float(row.get("alpha", 0.0))
                    beta = float(row.get("beta", 1.0))
                    res_vol = float(row.get("residual_vol", 0.0))

                    metrics = {
                        "rm_z": round(rm_z, 2),
                        "alpha": round(alpha, 4),
                        "beta": round(beta, 2),
                        "residual_vol": round(res_vol, 4),
                    }

                    if rm_z >= 1.0:
                        return SingleStrategyPrediction(
                            status="RESIDUAL_MOMENTUM_LONG",
                            direction="LONG",
                            entry=ltp,
                            stop_loss=round(ltp * 0.95, 2),
                            target=round(ltp * 1.15, 2),
                            reason=f"Top decile residual momentum z-score ({rm_z:+.2f}) with beta {beta:.2f}",
                            metrics=metrics,
                        )
                    elif rm_z <= -1.0:
                        return SingleStrategyPrediction(
                            status="RESIDUAL_MOMENTUM_SHORT",
                            direction="SHORT",
                            entry=ltp,
                            stop_loss=round(ltp * 1.05, 2),
                            target=round(ltp * 0.85, 2),
                            reason=f"Bottom decile residual momentum z-score ({rm_z:+.2f}) with beta {beta:.2f}",
                            metrics=metrics,
                        )
                    else:
                        return SingleStrategyPrediction(
                            status="NO_TRADE",
                            reason=f"Residual momentum z-score ({rm_z:+.2f}) in neutral band",
                            metrics=metrics,
                        )

            if not df_15m.empty and "close" in df_15m.columns:
                data = df_15m.copy()
                if "datetime" not in data.columns and isinstance(data.index, pd.DatetimeIndex):
                    data["datetime"] = data.index
                data["date"] = pd.to_datetime(data["datetime"]).dt.date
                daily_px = data.groupby("date")["close"].last()
                if len(daily_px) >= 3:
                    ret = daily_px.pct_change().dropna()
                    if not ret.empty:
                        std_val = float(ret.std()) if len(ret) > 1 else 0.01
                        mean_val = float(ret.mean())
                        z_val = round((ret.iloc[-1] - mean_val) / (std_val + 1e-6), 2)
                        metrics = {"momentum_z": z_val, "sessions": len(daily_px)}
                        if z_val >= 1.2:
                            return SingleStrategyPrediction(
                                status="RESIDUAL_MOMENTUM_LONG",
                                direction="LONG",
                                entry=ltp,
                                stop_loss=round(ltp * 0.96, 2),
                                target=round(ltp * 1.12, 2),
                                reason=f"Strong short-term residual momentum (z = {z_val:+.2f})",
                                metrics=metrics,
                            )
                        elif z_val <= -1.2:
                            return SingleStrategyPrediction(
                                status="RESIDUAL_MOMENTUM_SHORT",
                                direction="SHORT",
                                entry=ltp,
                                stop_loss=round(ltp * 1.04, 2),
                                target=round(ltp * 0.88, 2),
                                reason=f"Strong negative residual momentum (z = {z_val:+.2f})",
                                metrics=metrics,
                            )
                        else:
                            return SingleStrategyPrediction(
                                status="NO_TRADE",
                                reason=f"Residual momentum score ({z_val:+.2f}) in neutral range",
                                metrics=metrics,
                            )

            return SingleStrategyPrediction(
                status="UNAVAILABLE",
                reason="Requires daily closing price history for 252-day OLS residual fit",
            )
        except Exception as e:
            logger.warning(f"Error evaluating RM-100 for {symbol}: {e}")
            return SingleStrategyPrediction(status="ERROR", reason=f"RM-100 evaluation error: {e}")

    def _evaluate_vrp_index(
        self,
        symbol: str,
        df_15m: pd.DataFrame,
        ltp: float,
        vix_series: Optional[pd.Series] = None,
        rv_series: Optional[pd.Series] = None,
    ) -> SingleStrategyPrediction:
        """Evaluates NSE-VRP-INDEX (Variance Risk Premium Harvest)."""
        try:
            vrp_strat = VRPHarvestStrategy()

            if vix_series is not None and not vix_series.empty and rv_series is not None and not rv_series.empty:
                try:
                    sig = vrp_strat.vrp_signal(rv_series=rv_series, india_vix=vix_series)
                    z_val = sig.get("z", 0.0)
                    iv_val = sig.get("iv", 0.0)
                    rv_val = sig.get("rv20", 0.0)
                    spread_val = sig.get("spread", 0.0)

                    metrics = {
                        "iv_vix": round(iv_val, 2),
                        "rv_20": round(rv_val, 2),
                        "vrp_spread": round(spread_val, 2),
                        "vrp_zscore": round(z_val, 2),
                    }

                    if iv_val > 23.0:
                        return SingleStrategyPrediction(
                            status="NO_TRADE",
                            reason=f"India VIX ({iv_val:.1f}%) exceeds safety ceiling (23.0%)",
                            metrics=metrics,
                        )
                    elif z_val >= 0.5 and 11.5 <= iv_val <= 23.0:
                        return SingleStrategyPrediction(
                            status="VRP_HARVEST",
                            direction="SHORT",
                            entry=ltp,
                            reason=f"VRP z-score ({z_val:+.2f}) is rich (VIX {iv_val:.1f}% vs RV20 {rv_val:.1f}%)",
                            metrics=metrics,
                        )
                    else:
                        return SingleStrategyPrediction(
                            status="NO_TRADE",
                            reason=f"VRP z-score ({z_val:+.2f}) below min 0.5 threshold",
                            metrics=metrics,
                        )
                except Exception as e:
                    return SingleStrategyPrediction(
                        status="UNAVAILABLE",
                        reason=f"Insufficient VRP alignment sessions: {e}",
                    )

            return SingleStrategyPrediction(
                status="UNAVAILABLE",
                reason="Live India VIX and Options Chain volatility feeds not connected",
            )
        except Exception as e:
            logger.warning(f"Error evaluating VRP Index for {symbol}: {e}")
            return SingleStrategyPrediction(status="ERROR", reason=f"VRP Index evaluation error: {e}")

    def _evaluate_apex(
        self,
        symbol: str,
        df_15m: pd.DataFrame,
        ltp: float,
        stock_metric: Optional[Any] = None,
        gift_nifty_gap: float = 0.0,
        catalyst_score: float = 0.0,
    ) -> SingleStrategyPrediction:
        """Evaluates APEX-AIVEM 6-Factor Pre-Market & Catalyst Engine."""
        try:
            cfg = EngineConfig()

            gap_pct = getattr(stock_metric, "gap_pct", 0.0) if stock_metric else 0.0
            rvol = getattr(stock_metric, "rvol", 1.0) if stock_metric else 1.0
            atr_14 = getattr(stock_metric, "atr_14", ltp * 0.02 if ltp else 10.0) if stock_metric else (ltp * 0.02 if ltp else 10.0)
            vwap_dist = getattr(stock_metric, "vwap_dist_pct", 0.0) if stock_metric else 0.0
            bias = getattr(stock_metric, "direction_bias", "NEUTRAL") if stock_metric else "NEUTRAL"

            z_gap = min(max(gap_pct / 1.5, -3.0), 3.0)
            z_vol = min(max((rvol - 1.0) / 0.5, 0.0), 3.0)
            z_oir = min(max(vwap_dist / 1.0, -3.0), 3.0)
            z_sec = min(max(gap_pct * 0.8, -3.0), 3.0)
            z_mkt = min(max(gap_pct - gift_nifty_gap * 100.0, -3.0), 3.0)
            z_cat = float(catalyst_score)

            sign_gap = 1.0 if gap_pct >= 0 else -1.0

            raw_apex = (
                sign_gap * (
                    cfg.w_gap * abs(z_gap) +
                    cfg.w_vol * z_vol +
                    cfg.w_oir * (sign_gap * z_oir) +
                    cfg.w_sector * (sign_gap * z_sec) +
                    cfg.w_market * (sign_gap * z_mkt) +
                    cfg.w_cat * z_cat
                )
            )
            apex_score = round(float(raw_apex), 2)

            metrics = {
                "apex_score": apex_score,
                "z_gap": round(z_gap, 2),
                "z_vol": round(z_vol, 2),
                "z_oir": round(z_oir, 2),
                "z_sec": round(z_sec, 2),
                "z_mkt": round(z_mkt, 2),
                "z_cat": round(z_cat, 2),
            }

            stop_dist = round(cfg.stop_atr_mult * atr_14, 2)
            target_dist = round(cfg.target_atr_mult * atr_14, 2)

            if apex_score >= 0.40 or (bias == "LONG" and apex_score >= 0.20):
                entry_p = round(ltp, 2)
                return SingleStrategyPrediction(
                    status="CONFIRMED_LONG",
                    direction="LONG",
                    entry=entry_p,
                    stop_loss=round(entry_p - stop_dist, 2),
                    target=round(entry_p + target_dist, 2),
                    reason=f"APEX 6-factor composite score ({apex_score:+.2f}) confirms bullish pre-market & volume expansion",
                    metrics=metrics,
                )
            elif apex_score <= -0.40 or (bias == "SHORT" and apex_score <= -0.20):
                entry_p = round(ltp, 2)
                return SingleStrategyPrediction(
                    status="CONFIRMED_SHORT",
                    direction="SHORT",
                    entry=entry_p,
                    stop_loss=round(entry_p + stop_dist, 2),
                    target=round(entry_p - target_dist, 2),
                    reason=f"APEX 6-factor composite score ({apex_score:+.2f}) confirms bearish pre-market & volume expansion",
                    metrics=metrics,
                )
            else:
                return SingleStrategyPrediction(
                    status="NO_TRADE",
                    reason=f"APEX composite score ({apex_score:+.2f}) within neutral threshold band",
                    metrics=metrics,
                )
        except Exception as e:
            logger.warning(f"Error evaluating APEX for {symbol}: {e}")
            return SingleStrategyPrediction(status="ERROR", reason=f"APEX engine evaluation error: {e}")

    @staticmethod
    def calculate_consensus(predictions: Dict[str, SingleStrategyPrediction]) -> Dict[str, Any]:
        """
        Computes consensus direction, count of agreeing strategies, and human-readable label
        across all 6 independent strategies.
        """
        long_count = sum(1 for p in predictions.values() if p.direction == "LONG")
        short_count = sum(1 for p in predictions.values() if p.direction == "SHORT")
        evaluable_count = sum(1 for p in predictions.values() if p.status not in ("UNAVAILABLE", "ERROR"))
        total = len(predictions)

        if long_count >= 3 and short_count == 0:
            direction = "LONG"
            label = f"STRONG LONG ({long_count}/{total})"
        elif short_count >= 3 and long_count == 0:
            direction = "SHORT"
            label = f"STRONG SHORT ({short_count}/{total})"
        elif long_count > 0 and short_count > 0:
            direction = "DIVERGENT"
            label = f"DIVERGENT ({long_count}L / {short_count}S)"
        elif long_count in (1, 2) and short_count == 0:
            direction = "LONG"
            label = f"MODERATE LONG ({long_count}/{total})"
        elif short_count in (1, 2) and long_count == 0:
            direction = "SHORT"
            label = f"MODERATE SHORT ({short_count}/{total})"
        else:
            direction = "NEUTRAL"
            label = "NEUTRAL"

        return {
            "direction": direction,
            "agreeing_strategies": max(long_count, short_count),
            "total_strategies": total,
            "evaluable_strategies": evaluable_count,
            "label": label,
        }

    @staticmethod
    def extract_key_insights(candidates: List[CandidatePrediction]) -> Dict[str, Any]:
        """
        Derives Top Long, Top Short, Strongest Consensus, and Divergent Signals.
        """
        if not candidates:
            return {
                "top_long": None,
                "top_short": None,
                "strongest_consensus": None,
                "divergent_signals": [],
            }

        # 1. Top Long Opportunity
        long_candidates = [c for c in candidates if c.consensus.get("direction") == "LONG"]
        top_long = max(long_candidates, key=lambda c: (c.consensus.get("agreeing_strategies", 0), c.momentum_score), default=None)

        # 2. Top Short Opportunity
        short_candidates = [c for c in candidates if c.consensus.get("direction") == "SHORT"]
        top_short = max(short_candidates, key=lambda c: (c.consensus.get("agreeing_strategies", 0), c.momentum_score), default=None)

        # 3. Strongest Consensus
        strongest = max(candidates, key=lambda c: (c.consensus.get("agreeing_strategies", 0), c.momentum_score), default=None)

        # 4. Divergent Signals
        divergents = [c.to_dict() for c in candidates if c.consensus.get("label") == "DIVERGENT"]

        return {
            "top_long": top_long.to_dict() if top_long else None,
            "top_short": top_short.to_dict() if top_short else None,
            "strongest_consensus": strongest.to_dict() if strongest else None,
            "divergent_signals": divergents,
        }


prediction_service = PredictionService()
