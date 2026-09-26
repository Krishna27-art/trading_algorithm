"""
Order journal, normalized broker positions, and manual place/exit order
routes. Every order route requires the X-Shared-Secret header (see
backend/security.py) since these mutate real or paper broker state.
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from backend.security import verify_shared_secret
from broker.kite_adapter import get_active_kite

logger = logging.getLogger("backend_api.positions")

router = APIRouter()

_shared_paper_broker = None


def get_paper_broker():
    global _shared_paper_broker
    if _shared_paper_broker is None:
        from broker.paper_broker import PaperBrokerAdapter
        from config.settings import settings
        _shared_paper_broker = PaperBrokerAdapter(initial_capital=settings.risk.initial_capital)
        _shared_paper_broker.connect()
    return _shared_paper_broker


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
    Source of truth: Zerodha Kite Connect positions() when authenticated,
    or active paper broker simulator positions.
    Guarantees exactly one row per (exchange, tradingsymbol, product).
    """
    from execution.position_service import PositionService
    from database.db import DatabaseManager
    from config.settings import settings

    kite = get_active_kite()
    service = PositionService(kite_client=kite)

    broker_mode = "LIVE" if kite else "PAPER"
    raw_positions = []

    if kite:
        try:
            pos_dict = kite.positions()
            raw_positions = pos_dict.get("net", [])
        except Exception as e:
            logger.error(f"Error fetching live Kite positions: {e}")
            raw_positions = []
    else:
        paper = get_paper_broker()
        raw_positions = paper.get_positions()

    # Extract strategy SL / Target metadata from active trade entries in DB
    db = DatabaseManager(settings.db_path)
    trades = db.get_all_trades()
    open_trades = [t for t in trades if not t.get("exit_price")]
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


class PlaceOrderRequest(BaseModel):
    symbol: str = "NIFTY"
    direction: str = "BUY"  # BUY or SELL
    order_type: str = "LIMIT"
    price: Optional[float] = None
    quantity: int = 1
    mode: str = "PAPER"  # PAPER or LIVE


@router.post("/api/orders/place")
def place_order(
    req: PlaceOrderRequest,
    x_shared_secret: Optional[str] = Header(None, alias="X-Shared-Secret"),
):
    """Places entry order in PAPER mode (simulator) or LIVE mode (Zerodha Kite)."""
    verify_shared_secret(x_shared_secret)
    from broker.paper_broker import PaperBrokerAdapter
    from database.models import OrderDirection, OrderType
    from database.db import DatabaseManager
    from config.settings import settings
    from risk.risk_manager import RiskManager

    db = DatabaseManager(settings.db_path)
    trades = db.get_all_trades()
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Reconstruct current session risk state
    risk_manager = RiskManager(settings.risk)
    risk_manager.reset_daily_state(datetime.now().date())

    trades_today = [
        t for t in trades
        if (t.get("entry_time") or "").startswith(today_str) and t.get("symbol") == req.symbol
    ]
    risk_manager.daily_trades_count[req.symbol] = len(trades_today)

    realized_pnl_today = sum(
        (t.get("pnl_net") or 0.0) for t in trades
        if (t.get("exit_time") or "").startswith(today_str)
    )
    risk_manager.update_pnl(realized_pnl_delta=realized_pnl_today, capital=settings.risk.initial_capital)

    open_trades = [t for t in trades if not t.get("exit_price") and t.get("symbol") == req.symbol]
    has_same_direction_position = any(
        (t.get("direction") == "BUY" and req.direction.upper() == "BUY") or
        (t.get("direction") == "SELL" and req.direction.upper() == "SELL")
        for t in open_trades
    )

    # Strict Pre-Trade Risk Gate for entries
    approved, reason = risk_manager.validate_pre_trade(
        symbol=req.symbol,
        current_time=datetime.now().time(),
        quantity=req.quantity,
        capital=settings.risk.initial_capital,
        has_open_position=has_same_direction_position,
    )
    if not approved:
        logger.warning(f"Order rejected by pre-trade risk gate: {reason}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=reason,
        )

    direction = OrderDirection.BUY if req.direction.upper() == "BUY" else OrderDirection.SELL
    order_type = OrderType.LIMIT if req.order_type.upper() == "LIMIT" else OrderType.MARKET

    if req.mode == "LIVE":
        kite = get_active_kite()
        if not kite:
            raise HTTPException(status_code=400, detail="Zerodha Kite session is not active for live orders.")
        from broker.kite_adapter import KiteBrokerAdapter
        adapter = KiteBrokerAdapter()
        adapter.kite = kite
        adapter.is_connected = True
        record = adapter.place_order(
            symbol=req.symbol,
            direction=direction,
            order_type=order_type,
            quantity=req.quantity,
            price=req.price,
            tag="ALGO_LIVE",
        )
    else:
        paper = get_paper_broker()
        record = paper.place_order(
            symbol=req.symbol,
            direction=direction,
            order_type=order_type,
            quantity=req.quantity,
            price=req.price,
            tag="ALGO_PAPER",
        )

    risk_manager.record_trade_executed(req.symbol)
    db.save_order(record)

    return {"success": True, "order": record.dict()}


class ExitOrderRequest(BaseModel):
    symbol: str = "NIFTY"
    mode: str = "PAPER"


@router.post("/api/orders/exit")
def exit_order(
    req: ExitOrderRequest,
    x_shared_secret: Optional[str] = Header(None, alias="X-Shared-Secret"),
):
    """Squares off any active open position for the specified symbol immediately."""
    verify_shared_secret(x_shared_secret)
    from database.db import DatabaseManager
    from database.models import OrderDirection, OrderType
    from config.settings import settings

    db = DatabaseManager(settings.db_path)
    trades = db.get_all_trades()
    open_trades = [t for t in trades if not t.get("exit_price") and t.get("symbol") == req.symbol]

    if not open_trades:
        return {"success": True, "message": f"No open positions found for {req.symbol}."}

    trade = open_trades[0]
    exit_direction = OrderDirection.SELL if trade.get("direction") == "BUY" else OrderDirection.BUY
    qty = trade.get("quantity", 1)

    if req.mode == "LIVE":
        kite = get_active_kite()
        if not kite:
            raise HTTPException(status_code=400, detail="Zerodha Kite session is not active for live exit.")
        from broker.kite_adapter import KiteBrokerAdapter
        adapter = KiteBrokerAdapter()
        adapter.kite = kite
        adapter.is_connected = True
        record = adapter.place_order(
            symbol=req.symbol,
            direction=exit_direction,
            order_type=OrderType.MARKET,
            quantity=qty,
            tag="EXIT_LIVE",
        )
    else:
        paper = get_paper_broker()
        record = paper.place_order(
            symbol=req.symbol,
            direction=exit_direction,
            order_type=OrderType.MARKET,
            quantity=qty,
            tag="EXIT_PAPER",
        )

    # Record trade exit in DB
    from database.models import TradeRecord, ExitReason
    exit_price = record.average_fill_price or trade.get("entry_price", 0.0)
    entry_p = float(trade.get("entry_price", 0.0))
    pnl_gross = (exit_price - entry_p) * qty if trade.get("direction") == "BUY" else (entry_p - exit_price) * qty

    entry_t = trade.get("entry_time")
    if isinstance(entry_t, str):
        try:
            entry_t = datetime.fromisoformat(entry_t)
        except Exception:
            entry_t = datetime.now()
    elif not isinstance(entry_t, datetime):
        entry_t = datetime.now()

    trade_obj = TradeRecord(
        trade_id=trade["trade_id"],
        symbol=trade.get("symbol", req.symbol),
        direction=OrderDirection.BUY if trade.get("direction") == "BUY" else OrderDirection.SELL,
        entry_time=entry_t,
        entry_price=entry_p,
        exit_time=datetime.now(),
        exit_price=exit_price,
        quantity=qty,
        initial_stop=float(trade.get("initial_stop") or 0.0),
        initial_target=float(trade.get("initial_target") or 0.0),
        exit_reason=ExitReason.MANUAL,
        pnl_gross=pnl_gross,
        pnl_net=pnl_gross,
        is_paper=(req.mode == "PAPER"),
        notes="MANUAL_SQUARE_OFF",
    )
    db.record_trade_exit(trade_obj)
    db.save_order(record)

    return {"success": True, "message": f"Closed position on {req.symbol}", "order": record.dict()}
