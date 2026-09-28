"""
WebSocket Market Stream management routes.
Exposes /api/stream/start and /api/stream/stop to control the KiteTicker
connection managed by KiteBrokerAdapter.
"""

from fastapi import APIRouter, HTTPException, status

from broker.kite_adapter import kite_broker_adapter
from monitoring.logger import logger

router = APIRouter()


@router.post("/api/stream/start")
def start_stream(tokens: dict):
    """
    Start the KiteTicker WebSocket stream for the provided token-symbol mapping.
    Body: {"123456": "RELIANCE", "738561": "INFY", ...}
    Ticks are routed through the candle aggregator (15-minute bars) and made
    available to any registered strategy callbacks.
    """
    if not tokens:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must contain at least one {token: symbol} entry."
        )

    try:
        token_to_symbol = {int(k): str(v) for k, v in tokens.items()}

        def on_candle(candle_dict: dict, vwap: float):
            logger.debug(f"Candle closed: {candle_dict.get('symbol')} {candle_dict.get('datetime')} close={candle_dict.get('close')}")

        kite_broker_adapter.start_market_stream(
            token_to_symbol=token_to_symbol,
            on_candle_close=on_candle,
        )
        return {
            "status": "started",
            "subscribed_tokens": len(token_to_symbol),
            "symbols": list(token_to_symbol.values()),
        }
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Error starting stream: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start stream: {e}"
        )


@router.post("/api/stream/stop")
def stop_stream():
    """Stop the currently active KiteTicker WebSocket stream."""
    kite_broker_adapter.stop_market_stream()
    return {"status": "stopped"}


@router.get("/api/stream/status")
def stream_status():
    """Returns whether the KiteTicker stream is currently connected."""
    is_connected = (
        hasattr(kite_broker_adapter, "kws") and
        kite_broker_adapter.kws is not None
    )
    return {"connected": is_connected}
