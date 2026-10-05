"""
Lightweight real-time market data endpoint for the stock universe.

GET /api/market/prices fetches live quotes in batches from authenticated
Zerodha Kite Connect.

Data-integrity rules enforced here:
  * A missing quote field is returned as None. It is NEVER replaced by LTP
    (previously prev_close/open/vwap silently defaulted to LTP, which made
    change% read 0 and VWAP-distance read 0 for incomplete quotes).
  * A failed quote batch is reported explicitly (failed_batches, per-symbol
    DATA_UNAVAILABLE, response status PARTIAL / DATA_UNAVAILABLE).
  * Per-symbol timestamp is the EXCHANGE timestamp from the quote (or None),
    not the server's "now". Freshness is reported as LIVE or STALE.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from broker.kite_adapter import get_active_kite_with_diagnostics
from config.universe import StockUniverse, resolve_universe_tokens
from data.time_utils import IST, now_ist_iso, now_ist_naive

logger = logging.getLogger("backend_api.market")

router = APIRouter()

QUOTE_BATCH_SIZE = 150
# A quote whose exchange timestamp is older than this is reported STALE.
QUOTE_STALE_AFTER_SECONDS = 120


def _opt_float(value: Any) -> Optional[float]:
    """float(value) or None when absent/invalid. Zero is treated as unavailable
    only by callers that know zero is not a valid value for that field."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quote_timestamp(q_data: Dict[str, Any]) -> Optional[datetime]:
    """Return the exchange timestamp converted to naive IST."""
    ts = q_data.get("timestamp") or q_data.get("last_trade_time")

    if not isinstance(ts, datetime):
        return None

    if ts.tzinfo is not None:
        return ts.astimezone(IST).replace(tzinfo=None)

    return ts


def _unavailable_row(record, token, reason: str) -> Dict[str, Any]:
    return {
        "rank": record.market_cap_rank,
        "symbol": record.symbol,
        "name": record.name,
        "category": record.category,
        "token": token,
        "ltp": None,
        "prev_close": None,
        "open_price": None,
        "change": None,
        "change_pct": None,
        "volume": None,
        "vwap": None,
        "status": "DATA_UNAVAILABLE",
        "reason": reason,
        "timestamp": None,
    }


@router.get("/api/market/prices")
def get_market_prices() -> Dict[str, Any]:
    """Read-only live market prices for the stock universe (real Kite quotes only)."""
    now_iso = now_ist_iso()
    kite, auth_err = get_active_kite_with_diagnostics(force_validate=False)

    if not kite:
        return {
            "status": "AUTH_REQUIRED",
            "data_source": "NONE",
            "count": 0,
            "timestamp": now_iso,
            "message": auth_err or "Zerodha Kite Connect session is not authenticated.",
            "stocks": [],
        }

    all_records = StockUniverse().all_stocks
    symbols = [r.symbol for r in all_records]

    token_map = resolve_universe_tokens(kite_client=kite)

    client = getattr(kite, "kite", None) if not hasattr(kite, "quote") else kite
    if client is None:
        client = kite

    quotes: Dict[str, Any] = {}
    failed_symbols: Dict[str, str] = {}
    failed_batches: List[Dict[str, Any]] = []

    for i in range(0, len(symbols), QUOTE_BATCH_SIZE):
        chunk = symbols[i : i + QUOTE_BATCH_SIZE]
        try:
            chunk_quotes = client.quote([f"NSE:{sym}" for sym in chunk])
            if chunk_quotes:
                quotes.update(chunk_quotes)
        except Exception as e:
            logger.error(
                "Quote batch failed component=market.prices batch=[%d:%d] error=%s",
                i, i + QUOTE_BATCH_SIZE, e,
            )
            failed_batches.append({"start": i, "end": i + len(chunk), "error": type(e).__name__})
            for sym in chunk:
                failed_symbols[sym] = f"quote batch failed: {type(e).__name__}"

    now_naive = now_ist_naive()
    stocks_list: List[Dict[str, Any]] = []

    for record in all_records:
        sym = record.symbol
        q_data = quotes.get(f"NSE:{sym}")
        token = token_map.get(sym) or (q_data.get("instrument_token") if q_data else None)

        ltp = _opt_float(q_data.get("last_price")) if q_data else None
        if ltp is None or ltp <= 0:
            reason = failed_symbols.get(sym, "no quote returned for symbol")
            stocks_list.append(_unavailable_row(record, token, reason))
            continue

        ohlc = q_data.get("ohlc") or {}
        prev_close = _opt_float(ohlc.get("close"))
        open_price = _opt_float(ohlc.get("open"))
        if prev_close is not None and prev_close <= 0:
            prev_close = None
        avg_price = _opt_float(q_data.get("average_price"))
        vwap = avg_price if avg_price and avg_price > 0 else None  # never fall back to LTP
        raw_volume = q_data.get("volume")
        volume = int(raw_volume) if raw_volume is not None else None

        change = round(ltp - prev_close, 2) if prev_close is not None else None
        change_pct = (
            round(((ltp - prev_close) / prev_close) * 100, 2) if prev_close is not None else None
        )

        q_ts = _quote_timestamp(q_data)
        if q_ts is None:
            freshness = "STALE"  # cannot prove freshness without an exchange timestamp
        else:
            age = (now_naive - q_ts).total_seconds()
            freshness = "LIVE" if 0 <= age <= QUOTE_STALE_AFTER_SECONDS else "STALE"

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
                "status": freshness,
                "timestamp": q_ts.isoformat() if q_ts else None,
            }
        )

    live_count = sum(
        1 for s in stocks_list
        if s.get("status") == "LIVE"
    )

    stale_count = sum(
        1 for s in stocks_list
        if s.get("status") == "STALE"
    )

    unavailable_count = sum(
        1 for s in stocks_list
        if s.get("status") == "DATA_UNAVAILABLE"
    )

    if live_count == len(stocks_list):
        overall = "success"
    elif live_count > 0:
        overall = "PARTIAL"
    elif stale_count > 0:
        overall = "STALE"
    else:
        overall = "DATA_UNAVAILABLE"

    return {
        "status": overall,
        "data_source": (
            "REAL_KITE"
            if (live_count + stale_count) > 0
            else "NONE"
        ),
        "count": len(stocks_list),
        "available_count": live_count,
        "stale_count": stale_count,
        "unavailable_count": unavailable_count,
        "failed_batches": failed_batches,
        "timestamp": now_iso,
        "stocks": stocks_list,
    }
