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

STRATEGY_ALIASES = {"gap": "gap_fade", "rsi": "rsi_ema", "orb": "orb_sip"}

STRATEGY_REGISTRY = {
    "gap_fade": {
        "name": "Gap Fade",
        "description": (
            "Stock opens 1%+ away from yesterday's close. If the first 30 minutes move "
            "back against the gap, trade toward yesterday's close. Stop: first-30-min "
            "high/low. Best result in backtests (+0.31% net per trade)."
        ),
        "timeframe": "15m candles",
        "key_levels": ["Previous Close", "Gap %", "First 30m High", "First 30m Low"],
        "schedule": {"entry_start": "09:45", "entry_end": "09:45", "square_off_time": "15:15"},
        "risk_reward_ratio": None,
    },
    "rsi_ema": {
        "name": "RSI x 9 EMA",
        "description": (
            "RSI(14) crosses its 9-period EMA. Buy only when RSI is below 50 and price is "
            "above VWAP; short only when RSI is above 50 and price is below VWAP. Only on "
            "days when the 09:15 candle volume is 1.5x normal. Small edge in backtests."
        ),
        "timeframe": "15m candles",
        "key_levels": ["RSI 14", "RSI EMA 9", "VWAP", "Relative Volume"],
        "schedule": {"entry_start": "10:00", "entry_end": "14:15", "square_off_time": "15:15"},
        "risk_reward_ratio": 3.0,
    },
    "orb_sip": {
        "name": "ORB 30-min, Stocks in Play (experimental)",
        "description": (
            "Breakout of the 09:15-09:45 range, only when the 09:15 candle volume is 2.5x "
            "normal. Stop: middle of the range. Did NOT pass its backtest; paper trade only."
        ),
        "timeframe": "15m candles",
        "key_levels": ["Range High", "Range Low", "Relative Volume"],
        "schedule": {"entry_start": "10:00", "entry_end": "11:30", "square_off_time": "15:15"},
        "risk_reward_ratio": 2.0,
    },
}


def _canonical_strategy(name: Optional[str], default: str) -> str:
    key = (name or default or "gap_fade").lower()
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
            "market_open": settings.strategy.market_open.strftime("%H:%M") + " IST",
            "entry_start": strat_meta["schedule"]["entry_start"] + " IST",
            "entry_end": strat_meta["schedule"]["entry_end"] + " IST",
            "square_off_time": strat_meta["schedule"]["square_off_time"] + " IST",
            "hard_cutoff_time": strat_meta["schedule"]["square_off_time"] + " IST",
        },
        "risk_reward_ratio": strat_meta["risk_reward_ratio"],
        "breakeven_r_multiple": None,
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

