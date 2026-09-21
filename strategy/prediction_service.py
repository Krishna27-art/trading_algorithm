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


@dataclass
class SingleStrategyPrediction:
    status: str  # e.g. "LONG_BREAKOUT", "BULLISH_EXPANSION", "TRENDING_LONG", "NO_TRADE", "WAITING", "BUFFER_ZONE"
    direction: Optional[str] = None  # "LONG", "SHORT", or None
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    reason: str = ""
    levels: Optional[Dict[str, Any]] = None

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
        if self.levels:
            clean_levels = {}
            for k, v in self.levels.items():
                if isinstance(v, (np.floating, float)):
                    clean_levels[k] = round(float(v), 2)
                elif isinstance(v, (np.integer, int)):
                    clean_levels[k] = int(v)
                else:
                    clean_levels[k] = v
            d["levels"] = clean_levels
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
        return {
            "rank": self.rank,
            "symbol": self.symbol,
            "ltp": round(self.ltp, 2),
            "momentum_score": round(self.momentum_score, 1),
            "universe_bias": self.universe_bias,
            "predictions": {k: v.to_dict() for k, v in self.predictions.items()},
            "consensus": self.consensus,
        }


class PredictionService:
    """
    Evaluates real strategies across candidate instruments.
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
    ) -> Tuple[Dict[str, SingleStrategyPrediction], Dict[str, Any]]:
        """
        Runs real ORB, CPR, and Dual-EMA on the provided 15-minute historical & intraday DataFrame.
        Returns (predictions_map, consensus_dict).
        """
        inst = create_instrument_config_for_equity(symbol, token or 0)
        ltp = current_ltp or (float(df_15m["close"].iloc[-1]) if not df_15m.empty else 0.0)

        orb_pred = self._evaluate_orb(inst, df_15m, ltp)
        cpr_pred = self._evaluate_cpr(inst, df_15m, ltp)
        dual_ema_pred = self._evaluate_dual_ema(inst, df_15m, ltp)

        predictions = {
            "orb": orb_pred,
            "cpr": cpr_pred,
            "dual_ema": dual_ema_pred,
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

    @staticmethod
    def calculate_consensus(predictions: Dict[str, SingleStrategyPrediction]) -> Dict[str, Any]:
        """
        Computes consensus direction, count of agreeing strategies, and human-readable label.
        """
        long_count = sum(1 for p in predictions.values() if p.direction == "LONG")
        short_count = sum(1 for p in predictions.values() if p.direction == "SHORT")
        total = len(predictions)

        if long_count == 3:
            return {
                "direction": "LONG",
                "agreeing_strategies": 3,
                "total_strategies": total,
                "label": "UNANIMOUS LONG",
            }
        elif short_count == 3:
            return {
                "direction": "SHORT",
                "agreeing_strategies": 3,
                "total_strategies": total,
                "label": "UNANIMOUS SHORT",
            }
        elif long_count == 2 and short_count == 0:
            return {
                "direction": "LONG",
                "agreeing_strategies": 2,
                "total_strategies": total,
                "label": "STRONG LONG 2/3",
            }
        elif short_count == 2 and long_count == 0:
            return {
                "direction": "SHORT",
                "agreeing_strategies": 2,
                "total_strategies": total,
                "label": "STRONG SHORT 2/3",
            }
        elif long_count >= 1 and short_count >= 1:
            return {
                "direction": "DIVERGENT",
                "agreeing_strategies": max(long_count, short_count),
                "total_strategies": total,
                "label": "DIVERGENT",
            }
        elif long_count == 1 and short_count == 0:
            return {
                "direction": "LONG",
                "agreeing_strategies": 1,
                "total_strategies": total,
                "label": "MODERATE LONG 1/3",
            }
        elif short_count == 1 and long_count == 0:
            return {
                "direction": "SHORT",
                "agreeing_strategies": 1,
                "total_strategies": total,
                "label": "MODERATE SHORT 1/3",
            }
        else:
            return {
                "direction": "NEUTRAL",
                "agreeing_strategies": 0,
                "total_strategies": total,
                "label": "NEUTRAL",
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
