"""
Lightweight real-time market data endpoint for the stock universe.

GET /api/market/prices provides a read-only view over the canonical
LiveMarketState populated by the Kite streaming pipeline.
"""

import logging
import math
from typing import Any, Dict, Optional

from fastapi import APIRouter

from broker.kite_adapter import get_active_kite
from config.universe import StockUniverse
from data.time_utils import now_ist_iso, now_ist_naive
from streaming.live_market_state import live_market_state
from streaming.market_stream_manager import market_stream_manager

logger = logging.getLogger("backend_api.market")

router = APIRouter()

_universe = StockUniverse()
_universe_map: Dict[str, Any] = {
    record.symbol: record
    for record in _universe.all_stocks
}

STREAM_DATA_STALE_AFTER_SECONDS = 120


def _symbol_feed_is_fresh(
    symbol: str,
    stream_connected: bool,
    max_age_seconds: int = STREAM_DATA_STALE_AFTER_SECONDS,
) -> bool:
    if not stream_connected:
        return False
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


def _enrich(state_dict: Dict[str, Any], stream_connected: bool) -> Dict[str, Any]:
    symbol = state_dict.get("symbol", "")
    record = _universe_map.get(symbol)

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

    is_fresh = _symbol_feed_is_fresh(symbol, stream_connected)
    state_dict["data_fresh"] = is_fresh

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

    return state_dict


@router.get("/api/market/prices")
def get_market_prices() -> Dict[str, Any]:
    kite_client = get_active_kite()
    if kite_client is None:
        return {
            "status": "AUTH_REQUIRED",
            "market_data_status": "UNAVAILABLE",
            "data_source": "NONE",
            "stream_connected": False,
            "data_fresh": False,
            "count": 0,
            "timestamp": now_ist_iso(),
            "stocks": [],
        }

    try:
        stream_stat = market_stream_manager.get_status() or {}
    except Exception:
        logger.exception("component=market_api check=stream_status")
        stream_stat = {}

    stream_connected = stream_stat.get("connected") is True
    age = stream_stat.get("last_tick_age_seconds")
    stream_fresh = (
        stream_connected
        and isinstance(age, (int, float))
        and 0 <= age <= STREAM_DATA_STALE_AFTER_SECONDS
    )

    data_source = (
        "KITE_STREAM"
        if (stream_connected and stream_fresh)
        else ("STALE_STREAM" if stream_connected else "DISCONNECTED")
    )
    market_data_status = (
        "AVAILABLE"
        if (stream_connected and stream_fresh)
        else ("STALE" if stream_connected else "UNAVAILABLE")
    )

    states = live_market_state.get_all_symbols_state()

    stocks_list = [
        _enrich(state.to_dict(), stream_connected)
        for state in states.values()
    ]

    return {
        "status": "success",
        "market_data_status": market_data_status,
        "data_source": data_source,
        "stream_connected": stream_connected,
        "data_fresh": stream_fresh,
        "count": len(stocks_list),
        "timestamp": now_ist_iso(),
        "stocks": stocks_list,
    }
