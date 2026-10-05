"""
Canonical live-data routes: /api/stream/start|stop|status|signals|market.

Instrument tokens are always resolved server-side from the authoritative
universe. The previous client-supplied `tokens` override was removed: it let a
caller subscribe arbitrary/fabricated instrument tokens.
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status

from backend.security import verify_shared_secret
from streaming.live_market_state import live_market_state
from streaming.live_signal_engine import live_signal_engine
from streaming.market_stream_manager import market_stream_manager

logger = logging.getLogger("backend_api.stream")

router = APIRouter()


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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
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


@router.get("/api/stream/signals")
def stream_signals():
    """Return only signals backed by a fresh Kite stream."""
    stream_status = _stream_status()
    fresh = _stream_feed_is_fresh(stream_status)

    signals = live_signal_engine.get_all_predictions()

    return {
        "status": "success",
        "stream_state": stream_status.get("state", "UNKNOWN"),
        "stream_connected": stream_status.get("connected", False),
        "last_tick_time": stream_status.get("last_tick_time"),
        "last_tick_age_seconds": stream_status.get(
            "last_tick_age_seconds"
        ),
        "data_fresh": fresh,
        "signals": signals,
        "count": len(signals),
    }


@router.get("/api/stream/market")
def stream_market():
    """Return in-memory market state tagged with Kite stream freshness."""
    stream_status = _stream_status()
    fresh = _stream_feed_is_fresh(stream_status)

    symbols_state = live_market_state.get_all_symbols_state()

    return {
        "status": "success",
        "stream_state": stream_status.get("state", "UNKNOWN"),
        "stream_connected": stream_status.get("connected", False),
        "last_tick_time": stream_status.get("last_tick_time"),
        "last_tick_age_seconds": stream_status.get(
            "last_tick_age_seconds"
        ),
        "data_fresh": fresh,
        "instruments": {
            sym: state.to_dict()
            for sym, state in symbols_state.items()
        },
        "count": len(symbols_state),
    }
