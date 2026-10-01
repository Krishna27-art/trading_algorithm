"""
WebSocket Market Stream management routes.
Exposes /api/stream/start, /api/stream/stop, /api/stream/status, and /api/stream/signals
controlling and querying the streaming market pipeline.
"""

from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException, status

from monitoring.logger import logger
from streaming.live_market_state import live_market_state
from streaming.live_signal_engine import live_signal_engine
from streaming.market_stream_manager import market_stream_manager

router = APIRouter()


@router.post("/api/stream/start")
def start_stream(tokens: Optional[Dict[str, str]] = None):
    """
    Start the KiteTicker WebSocket stream.
    If tokens mapping is not provided or empty, authoritative universe tokens
    are automatically resolved.
    """
    token_to_symbol: Optional[Dict[int, str]] = None
    if tokens:
        try:
            token_to_symbol = {int(k): str(v) for k, v in tokens.items()}
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid token format: {e}",
            )

    try:
        result = market_stream_manager.start_stream(token_to_symbol=token_to_symbol)
        return {
            "status": "started",
            "subscribed_tokens": result.get("subscribed_tokens", 0),
            "symbols_count": result.get("symbols_count", 0),
        }
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Error starting stream: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start stream: {e}",
        )


@router.post("/api/stream/stop")
def stop_stream():
    """Stop the currently active KiteTicker WebSocket stream."""
    return market_stream_manager.stop_stream()


@router.get("/api/stream/status")
def stream_status():
    """Returns the comprehensive connection and health state of the KiteTicker stream."""
    return market_stream_manager.get_status()


@router.get("/api/stream/signals")
def stream_signals():
    """Returns the latest live strategy signals computed from the streaming market pipeline."""
    return {
        "status": "success",
        "signals": live_signal_engine.get_all_predictions(),
        "count": len(live_signal_engine.get_all_predictions()),
    }


@router.get("/api/stream/market")
def stream_market():
    """Returns the latest in-memory market states (LTP, VWAP, Candle count) for all streaming symbols."""
    symbols_state = live_market_state.get_all_symbols_state()
    return {
        "status": "success",
        "instruments": {sym: state.to_dict() for sym, state in symbols_state.items()},
        "count": len(symbols_state),
    }
