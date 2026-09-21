"""
Authoritative Zerodha Kite Instrument Resolver.
Resolves symbols, exchanges, and expiries to numeric instrument_tokens.
Caches instrument lists locally with TTL to minimize Kite API bandwidth.
"""

from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# Known canonical tokens for major indices on NSE
CANONICAL_INDEX_TOKENS = {
    "NIFTY": 256265,          # NSE:NIFTY 50
    "NIFTY 50": 256265,
    "BANKNIFTY": 260105,      # NSE:NIFTY BANK
    "NIFTY BANK": 260105,
    "FINNIFTY": 257801,       # NSE:NIFTY FIN SERVICE
    "MIDCPNIFTY": 288009,     # NSE:NIFTY MID SELECT
}


class InstrumentResolver:
    """
    Resolves symbols to numeric instrument_tokens using Kite's instrument master.
    """

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or (settings.base_dir / "data" / "cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._memory_cache: Dict[str, Dict[str, Any]] = {}

    def _get_cache_path(self, exchange: str) -> Path:
        return self.cache_dir / f"instruments_{exchange.lower()}.json"

    def get_instruments(self, kite_client: Any, exchange: str = "NSE") -> List[Dict[str, Any]]:
        """
        Fetches instrument dump from Kite or reads from local disk cache if < 24 hours old.
        """
        cache_file = self._get_cache_path(exchange)

        # Check in-memory cache
        if exchange in self._memory_cache:
            return list(self._memory_cache[exchange].values())

        # Check disk cache
        if cache_file.exists():
            try:
                mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
                if datetime.now() - mtime < timedelta(hours=24):
                    with open(cache_file, "r") as f:
                        data = json.load(f)
                        self._memory_cache[exchange] = {i["tradingsymbol"]: i for i in data}
                        return data
            except Exception as e:
                logger.warning(f"Failed to read disk cache for {exchange}: {e}")

        # Fetch from Kite if client available
        if not kite_client:
            return []

        try:
            logger.info(f"Downloading {exchange} instrument master from Zerodha Kite...")
            raw_instruments = kite_client.instruments(exchange)
            # Simplify dump to save disk space
            sanitized = []
            for inst in raw_instruments:
                sanitized.append({
                    "instrument_token": inst.get("instrument_token"),
                    "tradingsymbol": inst.get("tradingsymbol"),
                    "name": inst.get("name"),
                    "expiry": str(inst.get("expiry")) if inst.get("expiry") else None,
                    "strike": inst.get("strike"),
                    "lot_size": inst.get("lot_size", 1),
                    "instrument_type": inst.get("instrument_type"),
                    "segment": inst.get("segment"),
                    "exchange": exchange,
                })

            with open(cache_file, "w") as f:
                json.dump(sanitized, f)

            self._memory_cache[exchange] = {i["tradingsymbol"]: i for i in sanitized}
            logger.info(f"Cached {len(sanitized)} instruments for {exchange}.")
            return sanitized
        except Exception as e:
            logger.error(f"Failed to fetch {exchange} instrument dump from Kite: {e}")
            return []

    def resolve_token(
        self,
        symbol: str,
        exchange: str = "NSE",
        kite_client: Optional[Any] = None,
    ) -> Optional[int]:
        """
        Resolves symbol to numeric instrument_token.
        Handles:
        1. Canonical index symbols ("NIFTY", "BANKNIFTY")
        2. NSE Equities ("RELIANCE", "TCS")
        3. NFO Futures (e.g. Current Month NIFTY Futures)
        """
        sym_clean = symbol.strip().upper()

        # 1. Canonical index check
        if sym_clean in CANONICAL_INDEX_TOKENS:
            return CANONICAL_INDEX_TOKENS[sym_clean]

        # 2. Check cached/fetched instruments
        instruments = self.get_instruments(kite_client, exchange=exchange)

        # Exact match
        for inst in instruments:
            if inst.get("tradingsymbol") == sym_clean:
                return int(inst["instrument_token"])

        # 3. For NFO Futures when passed "NIFTY"
        if exchange == "NFO" and "NIFTY" in sym_clean:
            # Find nearest monthly futures
            fut_candidates = [
                i for i in instruments
                if i.get("name") == "NIFTY" and i.get("instrument_type") == "FUT"
            ]
            if fut_candidates:
                # Sort by expiry ascending
                fut_candidates.sort(key=lambda x: x.get("expiry") or "9999-12-31")
                return int(fut_candidates[0]["instrument_token"])

        return None


# Global singleton instance
instrument_resolver = InstrumentResolver()
