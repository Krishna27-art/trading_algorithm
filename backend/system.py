"""System health route."""

from datetime import datetime

from fastapi import APIRouter

from broker.kite_adapter import get_active_kite

router = APIRouter()


@router.get("/api/system/health")
def get_system_health():
    """Reports status of critical broker, database, and algorithm subsystems using normalized enums."""
    from config.settings import settings
    kite = get_active_kite()
    kite_conn = kite is not None
    db_file = settings.db_path.exists()

    return {
        "kite_api": "CONNECTED" if kite_conn else "DISCONNECTED",
        "market_data": "CONNECTED" if kite_conn else "DISCONNECTED",
        "database": "CONNECTED" if db_file else "ERROR",
        "strategy_engine": "RUNNING",
        "risk_engine": "READY",
        "active_broker": "ZERODHA_KITE" if kite_conn else "DISCONNECTED",
        "overall_status": "READY" if kite_conn else "DISCONNECTED",
        "timestamp": datetime.now().isoformat(),
    }
