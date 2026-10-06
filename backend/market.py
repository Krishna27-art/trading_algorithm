"""
Lightweight real-time market data endpoint for the stock universe.

GET /api/market/prices provides a read-only view over the canonical
LiveMarketState populated by the Kite streaming pipeline.
"""

import logging
import math
from typing import Any, Dict, Optional

from fastapi import APIRouter

from config.universe import StockUniverse
from data.time_utils import now_ist_iso
from streaming.live_market_state import live_market_state

logger = logging.getLogger("backend_api.market")

router = APIRouter()

# Load universe metadata once at module level.
# Failures here are allowed to propagate — if the universe file is missing
# the whole backend cannot function correctly anyway.
_universe = StockUniverse()
_universe_map: Dict[str, Any] = {
    record.symbol: record
    for record in _universe.all_stocks
}


def _enrich(state_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    Add fields required by StocksPage.jsx that are not emitted by
    LiveSymbolState.to_dict().

    Fields added:
        name          — company name from the universe JSON
        category      — large / mid / small from the universe JSON
        rank          — market_cap_rank from the universe JSON
        open_price    — alias of state_dict["open"] for frontend field name
        change        — ltp - open_price (None when data not yet available)
        change_pct    — 100 * change / open_price (None when unavailable)
        status        — "LIVE" when ltp is present, "DATA_UNAVAILABLE" otherwise
    """
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

    if (
        ltp is not None
        and open_price is not None
        and math.isfinite(ltp)
        and math.isfinite(open_price)
        and open_price > 0
    ):
        change = round(ltp - open_price, 4)
        change_pct = round(100.0 * change / open_price, 4)
        state_dict["change"] = change
        state_dict["change_pct"] = change_pct
        state_dict["status"] = "LIVE"
    else:
        state_dict["change"] = None
        state_dict["change_pct"] = None
        state_dict["status"] = "DATA_UNAVAILABLE" if ltp is None else "LIVE"

    return state_dict


@router.get("/api/market/prices")
def get_market_prices() -> Dict[str, Any]:
    """Read-only live market prices for the stock universe served from LiveMarketState."""
    states = live_market_state.get_all_symbols_state()

    stocks_list = [
        _enrich(state.to_dict())
        for state in states.values()
    ]

    return {
        "status": "success",
        "data_source": "KITE_STREAM",
        "count": len(stocks_list),
        "timestamp": now_ist_iso(),
        "stocks": stocks_list,
    }


