"""
Strategy state and universe scanner (signal-only).

Canonical live signals flow through:
    KiteTicker -> MarketStreamManager -> CandleAggregator -> LiveSignalEngine
    -> PredictionService -> /api/stream/signals -> Frontend

All legacy REST evaluation endpoints (/api/research/live and /api/strategy/telemetry)
have been removed.
"""

import logging
from datetime import datetime, time as dt_time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status

from broker.kite_adapter import get_active_kite
from data.time_utils import now_ist_iso, now_ist_naive

logger = logging.getLogger("backend_api.signals")

router = APIRouter()

STRATEGY_ALIASES = {"sit": "sector_impulse", "ssf": "ssf_l5_srm"}

STRATEGY_REGISTRY = {
    "cpr": {
        "name": "Central Pivot Range (CPR) Regime Breakout & Mean-Reversion",
        "description": "Dynamic pivot width regime detection (Narrow/Wide/Neutral) with boundary breakout entries",
        "timeframe": "15m candles",
        "key_levels": ["TC", "Pivot P", "BC", "R1", "S1", "VWAP"],
    },
    "dual_ema": {
        "name": "Adaptive Volatility-Buffered Dual-EMA Trend System",
        "description": "Fast 9-EMA / 21-EMA trend ribbon with ATR-scaled noise buffer to eliminate chop",
        "timeframe": "15m candles",
        "key_levels": ["EMA-9", "EMA-21", "ATR Buffer", "VWAP"],
    },
    "orb": {
        "name": "30-Minute Volatility-Filtered Opening Range Breakout (ORB)",
        "description": "Classic 09:15-09:45 Opening Range breakout with session VWAP confirmation",
        "timeframe": "15m candles",
        "key_levels": ["OR High", "OR Low", "VWAP"],
    },
    "apex": {
        "name": "APEX-AIVEM Pre-Market Catalyst & Momentum Engine",
        "description": "Composite score (gap/ATR, RVOL, VWAP, candle imbalance) with optional VIX/GIFT-Nifty/sector context",
        "timeframe": "15m candles",
        "key_levels": ["VWAP", "ATR Stop", "ATR Target", "Gap/ATR Ratio"],
    },
    "sector_impulse": {
        "name": "Sector Impulse Transmission (SIT)",
        "description": "Cross-sectional lead-lag transmission trading lagging peers after idiosyncratic volume shock in sector leader",
        "timeframe": "15m candles",
        "key_levels": ["Leader Return", "Idiosyncratic Shock", "Gap Sigma", "Time Stop"],
    },
    "ssf_l5_srm": {
        "name": "Single-Stock Futures L5 Microprice & Sector Residual Momentum (SSF-L5-SRM)",
        "description": "Level-5 order flow imbalance, microprice drift, futures basis z-score, and sector residual momentum",
        "timeframe": "Tick / L5 Depth / 15m Regime",
        "key_levels": ["Microprice Dev", "Basis Z-Score", "OFI Imbalance", "Parkinson Vol"],
    },
    "aou_oss": {
        "name": "Analytic Ornstein-Uhlenbeck Optimal-Stopping System (AOU-OSS)",
        "description": "Rolling 32-bar RVWAP spread, Kendall-corrected OU calibration, half-life & Garman-Klass volatility gates, with numerical Bertram optimal stopping boundaries",
        "timeframe": "15m candles",
        "key_levels": ["RVWAP", "Spread", "Equilibrium", "Long Boundary", "Short Boundary", "Stop Barrier"],
    },
}


def _canonical_strategy(name: Optional[str], default: str) -> str:
    key = (name or default or "cpr").lower()
    key = STRATEGY_ALIASES.get(key, key)
    if key not in STRATEGY_REGISTRY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown strategy '{name}'. Valid: {', '.join(STRATEGY_REGISTRY)}.",
        )
    return key


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@router.get("/api/strategy/state")
def get_strategy_state(strategy: Optional[str] = None):
    """Returns configuration and state of the selected strategy."""
    from config.settings import settings
    from data.market_calendar import MarketCalendar

    now = now_ist_naive()
    phase = MarketCalendar.get_session_phase(now.time()).value
    strat_key = _canonical_strategy(strategy, settings.active_strategy)
    strat_meta = STRATEGY_REGISTRY[strat_key]

    return {
        "strategy_key": strat_key,
        "strategy_name": strat_meta["name"],
        "strategy_description": strat_meta["description"],
        "key_levels": strat_meta["key_levels"],
        "symbol": settings.instruments[0].symbol,
        "exchange": settings.instruments[0].exchange,
        "instrument_type": settings.instruments[0].instrument_type.value,
        "lot_size": settings.instruments[0].lot_size,
        "session_phase": phase,
        "schedule": {
            "market_open": "09:15 IST",
            "entry_start": "09:45 IST",
            "entry_end": "13:30 IST",
            "square_off_time": "14:30 IST",
            "hard_cutoff_time": "15:10 IST",
        },
        "risk_reward_ratio": settings.strategy.risk_reward_ratio,
        "breakeven_r_multiple": settings.strategy.breakeven_r_multiple,
        "broker": "ZERODHA_KITE",
        "read_only": True,
    }


@router.get("/api/strategy/scanner")
def get_universe_scan(top_n: int = 5, refresh: bool = False):
    """Scans and ranks the universe using real batched Kite quotes."""
    from scanner.stock_ranker import StockUniverseScanner

    kite = get_active_kite()
    if not kite:
        return {
            "status": "AUTH_REQUIRED",
            "data_source": "NONE",
            "message": "Zerodha Kite Connect session is not authenticated. Please log in with Kite to scan real market quotes.",
            "candidates": [],
            "count": 0,
            "top_n": top_n,
            "timestamp": now_ist_iso(),
        }

    try:
        scanner = StockUniverseScanner()
        ranked, data_source = scanner.scan_universe(
            kite_client=kite,
            top_n=top_n,
            force_refresh_history=refresh,
            allow_synthetic=False,
        )
        summary = getattr(scanner, "last_pipeline_summary", {}) or {}
        return {
            "status": "success",
            "data_source": data_source,
            "timestamp": now_ist_iso(),
            "count": len(ranked),
            "top_n": top_n,
            "pipeline_summary": {
                "universe_count": summary.get("universe_count"),
                "tradable_count": summary.get("tradable_count"),
                "setup_count": summary.get("setup_count"),
                "strong_signal_count": summary.get("strong_signal_count"),
            },
            "scoring_weights": {
                "rvol_weight": 30,
                "gap_weight": 25,
                "volatility_weight": 25,
                "vwap_dist_weight": 20,
                "total_max": 100,
            },
            "candidates": [m.to_dict() for m in ranked],
        }
    except Exception:
        logger.exception("component=scanner action=scan_universe top_n=%s", top_n)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Scanner error. See server logs.",
        )
