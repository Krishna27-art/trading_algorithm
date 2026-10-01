"""
Lightweight real-time market data endpoint for the 300-stock universe.

Provides GET /api/market/prices, fetching live quotes in batches of 150 from
authenticated Zerodha Kite Connect without running heavy scanner/ATR calculations.
"""

from datetime import datetime
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from broker.kite_adapter import get_active_kite_with_diagnostics
from config.universe import StockUniverse, resolve_universe_tokens
from data.time_utils import now_ist_iso

logger = logging.getLogger("backend_api.market")

router = APIRouter()


@router.get("/api/market/prices")
def get_market_prices() -> Dict[str, Any]:
    """
    Lightweight read-only live market prices for the 300-stock universe.
    
    Loads the master 300-stock universe, resolves instrument tokens, and fetches
    real quotes in batches of 150 from Zerodha Kite Connect.
    """
    now_iso = now_ist_iso()
    kite, auth_err = get_active_kite_with_diagnostics(force_validate=False)

    if not kite:
        return {
            "status": "AUTH_REQUIRED",
            "data_source": "NONE",
            "count": 0,
            "timestamp": now_iso,
            "message": "Zerodha Kite Connect session is not authenticated.",
            "stocks": [],
        }

    universe = StockUniverse()
    all_records = universe.all_stocks
    symbols = [r.symbol for r in all_records]

    token_map = resolve_universe_tokens(kite_client=kite)

    # Fetch quotes in batches of 150
    quotes: Dict[str, Any] = {}
    batch_size = 150
    client = getattr(kite, "kite", None) if not hasattr(kite, "quote") else kite
    if client is None:
        client = kite

    for i in range(0, len(symbols), batch_size):
        chunk = symbols[i : i + batch_size]
        quote_instruments = [f"NSE:{sym}" for sym in chunk]
        try:
            chunk_quotes = client.quote(quote_instruments)
            if chunk_quotes:
                quotes.update(chunk_quotes)
        except Exception as e:
            logger.warning(f"Failed to fetch market quotes for batch [{i}:{i+batch_size}]: {e}")

    stocks_list: List[Dict[str, Any]] = []

    for record in all_records:
        sym = record.symbol
        q_key = f"NSE:{sym}"
        q_data = quotes.get(q_key)
        token = token_map.get(sym) or (q_data.get("instrument_token") if q_data else None)

        if not q_data or "last_price" not in q_data:
            stocks_list.append(
                {
                    "rank": record.market_cap_rank,
                    "symbol": sym,
                    "name": record.name,
                    "category": record.category,
                    "token": token,
                    "ltp": None,
                    "prev_close": None,
                    "open_price": None,
                    "change": None,
                    "change_pct": None,
                    "volume": 0,
                    "vwap": None,
                    "status": "DATA_UNAVAILABLE",
                    "timestamp": now_iso,
                }
            )
            continue

        ltp = float(q_data.get("last_price", 0.0))
        ohlc = q_data.get("ohlc", {})
        prev_close = float(ohlc.get("close", ltp))
        open_price = float(ohlc.get("open", ltp))
        volume = int(q_data.get("volume", 0))
        vwap = float(q_data.get("average_price", 0.0)) or ltp

        change = round(ltp - prev_close, 2) if prev_close else 0.0
        change_pct = round(((ltp - prev_close) / prev_close) * 100, 2) if prev_close else 0.0

        stocks_list.append(
            {
                "rank": record.market_cap_rank,
                "symbol": sym,
                "name": record.name,
                "category": record.category,
                "token": token,
                "ltp": ltp,
                "prev_close": prev_close,
                "open_price": open_price,
                "change": change,
                "change_pct": change_pct,
                "volume": volume,
                "vwap": vwap,
                "status": "LIVE",
                "timestamp": now_iso,
            }
        )

    return {
        "status": "success",
        "data_source": "REAL_KITE",
        "count": len(stocks_list),
        "timestamp": now_iso,
        "stocks": stocks_list,
    }
