"""System health route — every field is derived from observed state."""

import logging
from typing import Any, Dict

from fastapi import APIRouter

from broker.kite_adapter import get_active_kite
from data.time_utils import now_ist_iso
from streaming.live_signal_engine import live_signal_engine
from streaming.market_stream_manager import market_stream_manager

logger = logging.getLogger("backend_api.system")

router = APIRouter()

# Stream states (as reported by MarketStreamManager.get_status()["state"]).
_LIVE_STATES = {"LIVE", "CONNECTED", "RUNNING"}


def _classify(stream_state: str) -> str:
    s = (stream_state or "").upper()
    if s in _LIVE_STATES:
        return "LIVE"
    if "STALE" in s:
        return "STALE"
    if "ERROR" in s or "FAIL" in s:
        return "ERROR"
    return "DISCONNECTED"


@router.get("/api/system/health")
def get_system_health() -> Dict[str, Any]:
    """Reports status of broker, stream, database and signal-engine subsystems."""
    from config.settings import settings

    try:
        kite_conn = get_active_kite() is not None
    except Exception:
        logger.exception("component=system.health check=kite_session")
        kite_conn = False

    try:
        db_ok = settings.db_path.exists()
    except Exception:
        db_ok = False

    try:
        stream_status: Dict[str, Any] = market_stream_manager.get_status() or {}
        stream_state = str(stream_status.get("state", "UNKNOWN"))
        stream_error = None
    except Exception as exc:
        logger.exception("component=system.health check=stream_status")
        stream_status, stream_state, stream_error = {}, "ERROR", type(exc).__name__

    stream_class = _classify(stream_state)

    try:
        prediction_count = len(live_signal_engine.get_all_predictions())
        engine_state = "PRODUCING_SIGNALS" if prediction_count > 0 else (
            "IDLE" if stream_class == "LIVE" else "NOT_RUNNING"
        )
    except Exception:
        logger.exception("component=system.health check=signal_engine")
        prediction_count, engine_state = 0, "ERROR"

    if not kite_conn:
        overall = "DISCONNECTED"
    elif stream_class == "LIVE" and engine_state != "ERROR":
        overall = "LIVE"
    elif stream_class in ("STALE", "ERROR"):
        overall = stream_class
    elif engine_state == "ERROR":
        overall = "ERROR"
    else:
        overall = "STANDBY"  # authenticated, stream not started

    return {
        "backend": "ONLINE",
        "kite_api": "CONNECTED" if kite_conn else "DISCONNECTED",
        "market_data": "CONNECTED" if kite_conn else "DISCONNECTED",
        "market_stream": stream_state,
        "market_stream_error": stream_error,
        "database": "CONNECTED" if db_ok else "ERROR",
        "strategy_engine": engine_state,
        "signal_count": prediction_count,
        # Signal-only system: there is no risk/execution engine to report on.
        "risk_engine": "NOT_APPLICABLE",
        "active_broker": "ZERODHA_KITE" if kite_conn else "DISCONNECTED",
        "overall_status": overall,
        "timestamp": now_ist_iso(),
    }
