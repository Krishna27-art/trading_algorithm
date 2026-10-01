"""
Read-Only Broker Positions & Order Journal Routes.
Provides normalized live broker positions directly from Zerodha Kite Connect
and queries historical trade journal records from SQLite.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter

from broker.kite_adapter import get_active_kite
from portfolio.position_service import PositionService

logger = logging.getLogger("backend_api.positions")

router = APIRouter()


@router.get("/api/strategy/orders")
def get_strategy_orders():
    """Fetches order history records from SQLite database."""
    from database.db import DatabaseManager
    from config.settings import settings

    db = DatabaseManager(settings.db_path)
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 50")
        rows = cursor.fetchall()
        return {"orders": [dict(r) for r in rows]}


@router.get("/api/portfolio/positions")
def get_portfolio_positions():
    """
    Returns normalized, deduplicated broker net positions.
    Source of truth: Zerodha Kite Connect positions() when authenticated.
    Guarantees exactly one row per (exchange, tradingsymbol, product).
    """
    from database.db import DatabaseManager
    from config.settings import settings

    kite = get_active_kite()
    service = PositionService(kite_client=kite)

    broker_mode = "LIVE" if kite else "DISCONNECTED"
    raw_positions = []

    if kite:
        try:
            pos_dict = kite.positions()
            raw_positions = pos_dict.get("net", [])
        except Exception as e:
            logger.error(f"Error fetching live Kite positions: {e}")
            raw_positions = []

    # Extract strategy SL / Target metadata from active trade entries in DB
    db = DatabaseManager(settings.db_path)
    open_trades = db.get_open_live_trades()
    strategy_meta = {}
    for t in open_trades:
        sym = t.get("symbol")
        if sym and sym not in strategy_meta:
            strategy_meta[sym] = {
                "stop_loss": t.get("initial_stop"),
                "target": t.get("initial_target"),
                "trade_id": t.get("trade_id"),
            }

    # Normalize, deduplicate, and validate positions
    normalized = service.normalize_positions(raw_positions, strategy_positions=strategy_meta)
    # Only return open positions or positions with non-zero activity
    pos_dicts = [p.to_dict() for p in normalized if p.quantity != 0]

    total_unrealised = sum(p["unrealised_pnl"] for p in pos_dicts)
    total_realised = sum(p["realised_pnl"] for p in pos_dicts)
    total_pnl = sum(p["pnl"] for p in pos_dicts)

    return {
        "status": "success",
        "broker": broker_mode,
        "authenticated": bool(kite),
        "count": len(pos_dicts),
        "total_unrealised_pnl": round(total_unrealised, 2),
        "total_realised_pnl": round(total_realised, 2),
        "total_pnl": round(total_pnl, 2),
        "positions": pos_dicts,
        "timestamp": datetime.now().isoformat(),
    }
