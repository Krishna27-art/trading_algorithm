"""System health route — every field is derived from observed state."""

from datetime import datetime
import logging
import sqlite3
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
MARKET_DATA_STALE_AFTER_SECONDS = 120


def _has_fresh_market_feed(
    kite_connected: bool,
    stream_status: Dict[str, Any],
) -> bool:
    if not kite_connected:
        return False

    if stream_status.get("connected") is not True:
        return False

    age = stream_status.get("last_tick_age_seconds")

    if not isinstance(age, (int, float)):
        return False

    if age < 0:
        return False

    return age <= MARKET_DATA_STALE_AFTER_SECONDS


def _fresh_prediction_timestamp(
    value: Any,
    max_age_seconds: float = MARKET_DATA_STALE_AFTER_SECONDS,
) -> bool:
    if not value:
        return False

    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False

    if ts.tzinfo is not None:
        now = datetime.now(ts.tzinfo)
    else:
        from data.time_utils import now_ist_naive
        now = now_ist_naive()

    age = (now - ts).total_seconds()

    return 0 <= age <= max_age_seconds


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
        conn = sqlite3.connect(
            f"file:{settings.db_path}?mode=ro",
            uri=True,
            timeout=2,
        )
        try:
            conn.execute("SELECT 1")
            db_ok = True
        finally:
            conn.close()
    except Exception:
        logger.exception(
            "component=system.health check=database"
        )
        db_ok = False

    try:
        stream_status: Dict[str, Any] = (
            market_stream_manager.get_status() or {}
        )
        stream_state = str(
            stream_status.get(
                "state",
                "UNKNOWN",
            )
        )
        stream_error = stream_status.get(
            "last_error",
            stream_status.get("error"),
        )
    except Exception as exc:
        logger.exception("component=system.health check=stream_status")
        stream_status, stream_state, stream_error = {}, "ERROR", type(exc).__name__

    stream_class = _classify(stream_state)

    market_feed_fresh = _has_fresh_market_feed(
        kite_connected=kite_conn,
        stream_status=stream_status,
    )

    try:
        predictions = (
            live_signal_engine.get_all_predictions()
            or {}
        )

        prediction_count = len(predictions)

        producing_signal_count = 0

        for prediction in predictions.values():
            if not isinstance(prediction, dict):
                continue

            direction = prediction.get("direction")
            status = prediction.get("status")

            if direction not in {"LONG", "SHORT"}:
                continue

            if status in {
                "UNAVAILABLE",
                "ERROR",
                "NO_TRADE",
                "WAITING",
            }:
                continue

            if not market_feed_fresh:
                continue

            if not _fresh_prediction_timestamp(
                prediction.get("ltp_timestamp")
            ):
                continue

            producing_signal_count += 1

        engine_state = (
            "PRODUCING_SIGNALS"
            if producing_signal_count > 0
            else (
                "IDLE"
                if market_feed_fresh
                else "STALE"
                if stream_status.get("connected") is True
                else "NOT_RUNNING"
            )
        )
    except Exception:
        logger.exception(
            "component=system.health check=signal_engine"
        )
        prediction_count, producing_signal_count, engine_state = 0, 0, "ERROR"

    if not kite_conn:
        overall = "DISCONNECTED"
    elif engine_state == "ERROR":
        overall = "ERROR"
    elif stream_class == "ERROR":
        overall = "ERROR"
    elif market_feed_fresh:
        overall = "LIVE"
    elif stream_status.get("connected") is True:
        overall = "STALE"
    else:
        overall = "STANDBY"  # authenticated, stream not started

    return {
        "backend": "ONLINE",
        "kite_api": "CONNECTED" if kite_conn else "DISCONNECTED",
        "market_data": (
            "CONNECTED"
            if market_feed_fresh
            else (
                "STALE"
                if kite_conn and stream_status.get("connected") is True
                else "DISCONNECTED"
            )
        ),
        "market_stream": stream_state,
        "market_stream_error": stream_error,
        "database": "CONNECTED" if db_ok else "ERROR",
        "strategy_engine": engine_state,
        "signal_count": prediction_count,
        "active_signal_count": producing_signal_count,
        # Read-only pre-signal risk validator is active in PredictionService.
        "risk_engine": "ACTIVE",
        "active_broker": "ZERODHA_KITE" if kite_conn else "DISCONNECTED",
        "overall_status": overall,
        "timestamp": now_ist_iso(),
    }
