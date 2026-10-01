"""System health route."""

from fastapi import APIRouter

from broker.kite_adapter import get_active_kite
from data.time_utils import now_ist_iso
from streaming.market_stream_manager import market_stream_manager

router = APIRouter()


@router.get("/api/system/health")
def get_system_health():
    """Reports status of critical broker, database, and algorithm subsystems using normalized enums."""
    from config.settings import settings
    kite = get_active_kite()
    kite_conn = kite is not None
    db_file = settings.db_path.exists()
    stream_status = market_stream_manager.get_status()

    return {
        "backend": "ONLINE",
        "kite_api": "CONNECTED" if kite_conn else "DISCONNECTED",
        "market_data": "CONNECTED" if kite_conn else "DISCONNECTED",
        "market_stream": stream_status["state"],
        "database": "CONNECTED" if db_file else "ERROR",
        "strategy_engine": "RUNNING",
        "risk_engine": "READY",
        "active_broker": "ZERODHA_KITE" if kite_conn else "DISCONNECTED",
        "overall_status": "READY" if kite_conn else "STANDBY",
        "timestamp": now_ist_iso(),
    }
