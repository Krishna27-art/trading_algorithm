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

from backend.broker.kite_adapter import get_active_kite
from backend.data.time_utils import now_ist_iso, now_ist_naive

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
    "crsd": {
        "name": "Cross-Sectional Residual Shock Divergence",
        "description": (
            "Intraday market-neutral residual divergence "
            "strategy using a target stock and peer hedge basket."
        ),
        "timeframe": "15m candles",
        "key_levels": [
            "Residual Z-Score",
            "Cross-Sectional Z-Score",
            "Entry Z",
            "Exit Z",
            "Residual Spread",
            "Hedge Basket",
            "Risk Scale",
        ],
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
    from backend.config.settings import settings
    from backend.config.universe import StockUniverse
    from backend.data.time_utils import MarketCalendar

    now = now_ist_naive()
    phase = MarketCalendar.get_session_phase(now.time()).value
    strat_key = _canonical_strategy(strategy, settings.active_strategy)
    strat_meta = STRATEGY_REGISTRY[strat_key]

    return {
        "strategy_key": strat_key,
        "strategy_name": strat_meta["name"],
        "strategy_description": strat_meta["description"],
        "key_levels": strat_meta["key_levels"],
        "symbol": None,
        "exchange": None,
        "instrument_type": None,
        "lot_size": None,
        "scope": "LIVE_UNIVERSE",
        "universe_count": len(StockUniverse().all_stocks),
        "configured_instrument": settings.instruments[0].symbol,
        "configured_exchange": settings.instruments[0].exchange,
        "session_phase": phase,
        "schedule": {
            "market_open": (
                settings.strategy.market_open.strftime("%H:%M")
                + " IST"
            ),
            "entry_start": (
                settings.strategy.entry_start.strftime("%H:%M")
                + " IST"
            ),
            "entry_end": (
                settings.strategy.entry_end.strftime("%H:%M")
                + " IST"
            ),
            "square_off_time": (
                settings.strategy.square_off_time.strftime("%H:%M")
                + " IST"
            ),
            "hard_cutoff_time": (
                settings.strategy.hard_cutoff_time.strftime("%H:%M")
                + " IST"
            ),
        },
        "risk_reward_ratio": settings.strategy.risk_reward_ratio,
        "breakeven_r_multiple": settings.strategy.breakeven_r_multiple,
        "broker": "ZERODHA_KITE",
        "read_only": True,
    }


@router.get("/api/strategy/scanner")
def get_universe_scan(top_n: int = 5, refresh: bool = False):
    """Scans and ranks the universe using real batched Kite quotes."""
    from backend.scanner.stock_ranker import StockUniverseScanner

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
        ranked_all, data_source = scanner.scan_universe(
            kite_client=kite,
            top_n=0,
            force_refresh_history=refresh,
            allow_synthetic=False,
        )
        ranked = (
            ranked_all[:top_n] if top_n > 0 else ranked_all
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
            "candidates": [m.to_dict() for m in ranked],
        }
    except Exception:
        logger.exception("component=scanner action=scan_universe top_n=%s", top_n)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Scanner error. See server logs.",
        )


@router.get("/api/strategy/performance")
def get_strategy_performance(
    period: str = "TODAY",
    strategy: Optional[str] = None,
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
):
    """
    Returns dynamically aggregated strategy performance metrics directly
    from SQLite signal_events (Day, Week, Month, Year, All-time).
    """
    from backend.database.db import db_manager

    strat_key = None
    if strategy and strategy.lower() != "all":
        strat_key = _canonical_strategy(strategy, default=strategy)

    try:
        perf = db_manager.get_strategy_performance(
            period=period,
            strategy=strat_key,
            symbol=symbol,
            direction=direction,
        )

        # Enrich strategies list with registry metadata
        enriched_strategies = []
        registered_keys_seen = set()

        for st in perf.get("strategies", []):
            sk = st.get("strategy")
            registered_keys_seen.add(sk)
            meta = STRATEGY_REGISTRY.get(sk, {})
            st_copy = dict(st)
            st_copy["name"] = meta.get("name", sk.upper())
            st_copy["description"] = meta.get("description", "")
            enriched_strategies.append(st_copy)

        # If no filter by specific strategy, ensure all registered strategies appear
        if not strat_key and not symbol:
            for rk, rmeta in STRATEGY_REGISTRY.items():
                if rk not in registered_keys_seen:
                    enriched_strategies.append({
                        "strategy": rk,
                        "name": rmeta.get("name", rk.upper()),
                        "description": rmeta.get("description", ""),
                        "signals": 0,
                        "wins": 0,
                        "losses": 0,
                        "expired": 0,
                        "ambiguous": 0,
                        "pending": 0,
                        "accuracy": 0.0,
                        "avg_return": 0.0,
                        "avg_mfe": 0.0,
                        "avg_mae": 0.0,
                        "long_signals": 0,
                        "long_wins": 0,
                        "long_losses": 0,
                        "long_accuracy": 0.0,
                        "short_signals": 0,
                        "short_wins": 0,
                        "short_losses": 0,
                        "short_accuracy": 0.0,
                    })

        perf["strategies"] = enriched_strategies
        return {
            "status": "success",
            "timestamp": now_ist_iso(),
            **perf,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("component=signals action=get_strategy_performance")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Performance aggregation error: {exc}",
        )


@router.get("/api/strategy/journal")
def get_signal_journal(
    period: Optional[str] = "TODAY",
    strategy: Optional[str] = None,
    symbol: Optional[str] = None,
    outcome: Optional[str] = None,
    limit: int = 100,
):
    """
    Returns individual actionable signal events recorded in SQLite.
    """
    from backend.database.db import db_manager

    strat_key = None
    if strategy and strategy.lower() != "all":
        strat_key = _canonical_strategy(strategy, default=strategy)

    try:
        signals = db_manager.get_signal_history(
            limit=limit,
            strategy=strat_key,
            symbol=symbol,
            period=period,
            outcome=outcome,
        )
        return {
            "status": "success",
            "timestamp": now_ist_iso(),
            "count": len(signals),
            "period": period,
            "signals": signals,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("component=signals action=get_signal_journal")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Signal journal retrieval error: {exc}",
        )

