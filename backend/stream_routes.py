"""
Canonical live-data routes: /api/stream/start|stop|status|signals|market.

Instrument tokens are always resolved server-side from the authoritative
universe. The previous client-supplied `tokens` override was removed: it let a
caller subscribe arbitrary/fabricated instrument tokens.
"""

import logging
import math
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from config.universe import StockUniverse
from data.time_utils import now_ist_naive
from backend.security import verify_shared_secret
from streaming.live_market_state import live_market_state
from streaming.live_signal_engine import live_signal_engine
from streaming.market_stream_manager import market_stream_manager

logger = logging.getLogger("backend_api.stream")

router = APIRouter()

# Universe metadata — loaded once at module startup.
_universe = StockUniverse()
_universe_map: Dict[str, Any] = {
    record.symbol: record
    for record in _universe.all_stocks
}


STREAM_DATA_STALE_AFTER_SECONDS = 120


def _stream_status() -> Dict[str, Any]:
    try:
        return market_stream_manager.get_status() or {}
    except Exception:
        logger.exception("component=stream_routes check=stream_status")
        return {}


def _stream_feed_is_fresh(stream_status: Dict[str, Any]) -> bool:
    if stream_status.get("connected") is not True:
        return False

    age = stream_status.get("last_tick_age_seconds")

    if not isinstance(age, (int, float)):
        return False

    return 0 <= age <= STREAM_DATA_STALE_AFTER_SECONDS


def _symbol_feed_is_fresh(
    symbol: str,
    max_age_seconds: int = STREAM_DATA_STALE_AFTER_SECONDS,
) -> bool:
    state = live_market_state.get_symbol_state(symbol)
    if state is None:
        return False

    ts = state.last_tick_time
    if ts is None:
        return False

    now = now_ist_naive()
    if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)
    age = (now - ts).total_seconds()

    return 0 <= age <= max_age_seconds


def _stream_state() -> str:
    try:
        return str(
            (market_stream_manager.get_status() or {})
            .get("state", "UNKNOWN")
        )
    except Exception:
        logger.exception("component=stream_routes check=stream_status")
        return "ERROR"


@router.post("/api/stream/start", dependencies=[Depends(verify_shared_secret)])
def start_stream() -> Dict[str, Any]:
    """Start the KiteTicker stream with authoritative universe tokens."""
    try:
        result = market_stream_manager.start_stream(token_to_symbol=None)
        return {
            "status": "started",
            "subscribed_tokens": result.get("subscribed_tokens", 0),
            "symbols_count": result.get("symbols_count", 0),
        }
    except RuntimeError as e:
        msg = str(e)
        if "authenticate" in msg.lower() or "active zerodha" in msg.lower() or "auth" in msg.lower():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=msg)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=msg)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        logger.exception("component=stream_routes action=start_stream")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start stream. See server logs.",
        )


@router.post("/api/stream/stop", dependencies=[Depends(verify_shared_secret)])
def stop_stream():
    """Stop the currently active KiteTicker stream."""
    return market_stream_manager.stop_stream()


@router.get("/api/stream/status")
def stream_status():
    """Connection and health state of the KiteTicker stream."""
    return market_stream_manager.get_status()


from datetime import datetime

SIGNAL_DATA_STALE_AFTER_SECONDS = 1800


def _signal_is_fresh(
    sig: Dict[str, Any],
    max_age_seconds: int = SIGNAL_DATA_STALE_AFTER_SECONDS,
) -> bool:
    if not isinstance(sig, dict):
        return False
    ts_str = sig.get("candle_timestamp") or sig.get("timestamp") or sig.get("ltp_timestamp")
    if not ts_str:
        return False
    try:
        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False

    now = now_ist_naive()
    if ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)

    if ts.date() != now.date():
        return False

    age = (now - ts).total_seconds()
    return 0 <= age <= max_age_seconds


@router.get("/api/stream/signals")
def stream_signals():
    """Return only signals backed by a fresh Kite stream, fresh symbol ticks, and fresh signal timestamps."""
    stream_stat = _stream_status()
    stream_connected = stream_stat.get("connected") is True

    all_signals = (
        live_signal_engine.get_all_predictions()
        if stream_connected
        else {}
    )

    signals = {
        sym: sig
        for sym, sig in all_signals.items()
        if _symbol_feed_is_fresh(sym, STREAM_DATA_STALE_AFTER_SECONDS)
        and _signal_is_fresh(sig, SIGNAL_DATA_STALE_AFTER_SECONDS)
    }

    fresh = len(signals) > 0 and _stream_feed_is_fresh(stream_stat)

    return {
        "status": "success",
        "stream_state": stream_stat.get("state", "UNKNOWN"),
        "stream_connected": stream_connected,
        "last_tick_time": stream_stat.get("last_tick_time"),
        "last_tick_age_seconds": stream_stat.get(
            "last_tick_age_seconds"
        ),
        "data_fresh": fresh,
        "signals": signals,
        "count": len(signals),
    }


@router.get("/api/stream/market")
def stream_market():
    """Return in-memory market state tagged with per-symbol Kite stream freshness."""
    stream_stat = _stream_status()
    stream_connected = stream_stat.get("connected") is True

    symbols_state = live_market_state.get_all_symbols_state() if stream_connected else {}

    instruments = {}
    for sym, state in symbols_state.items():
        state_dict = state.to_dict()
        is_fresh = _symbol_feed_is_fresh(sym, STREAM_DATA_STALE_AFTER_SECONDS)
        state_dict["data_fresh"] = is_fresh

        # Enrich with universe metadata and computed fields.
        record = _universe_map.get(sym)
        if record is not None:
            state_dict["name"] = record.name
            state_dict["category"] = record.category
            state_dict["rank"] = record.market_cap_rank
        else:
            state_dict.setdefault("name", "")
            state_dict.setdefault("category", "")
            state_dict.setdefault("rank", None)

        ltp: Optional[float] = state_dict.get("ltp")
        open_price: Optional[float] = state_dict.get("open")
        state_dict["open_price"] = open_price

        if (
            ltp is not None
            and open_price is not None
            and math.isfinite(ltp)
            and math.isfinite(open_price)
            and open_price > 0
        ):
            change = round(ltp - open_price, 4)
            state_dict["change"] = change
            state_dict["change_pct"] = round(100.0 * change / open_price, 4)
            state_dict["status"] = "LIVE" if is_fresh else "STALE"
        else:
            state_dict["change"] = None
            state_dict["change_pct"] = None
            state_dict["status"] = "DATA_UNAVAILABLE" if ltp is None else ("LIVE" if is_fresh else "STALE")

        instruments[sym] = state_dict

    return {
        "status": "success",
        "stream_state": stream_stat.get("state", "UNKNOWN"),
        "stream_connected": stream_connected,
        "last_tick_time": stream_stat.get("last_tick_time"),
        "last_tick_age_seconds": stream_stat.get(
            "last_tick_age_seconds"
        ),
        "data_fresh": _stream_feed_is_fresh(stream_stat),
        "instruments": instruments,
        "count": len(instruments),
    }

