"""
Authoritative Zerodha Kite Instrument Resolver.
Resolves symbols, exchanges, and expiries to numeric instrument_tokens.
Caches instrument lists locally with TTL to minimize Kite API bandwidth.
"""

from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    "INDIA VIX": 264969,      # NSE:INDIA VIX
    "INDIAVIX": 264969,
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
            if hasattr(kite_client, "instruments"):
                client = kite_client
            else:
                client = getattr(kite_client, "kite", kite_client)
            raw_instruments = client.instruments(exchange) if (client and hasattr(client, "instruments")) else []
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

        if sym_clean in CANONICAL_INDEX_TOKENS:
            return CANONICAL_INDEX_TOKENS[sym_clean]

        instruments = self.get_instruments(kite_client, exchange=exchange)

        for inst in instruments:
            if inst.get("tradingsymbol") == sym_clean:
                return int(inst["instrument_token"])

        if exchange == "NFO" and "NIFTY" in sym_clean:
            fut_candidates = [
                i for i in instruments
                if i.get("name") == "NIFTY" and i.get("instrument_type") == "FUT"
            ]
            if fut_candidates:
                fut_candidates.sort(key=lambda x: x.get("expiry") or "9999-12-31")
                return int(fut_candidates[0]["instrument_token"])

        return None

    def resolve_universe(
        self,
        symbols: List[str],
        kite_client: Optional[Any] = None,
        cache_path: Optional[Path] = None,
        force_refresh: bool = False,
    ) -> Tuple[Dict[str, int], List[str]]:
        """
        Resolves symbols from the 300-stock universe to numeric instrument_tokens
        using Kite's NSE instrument master.
        Caches to data/cache/universe_300_tokens.json.
        Returns (token_map: Dict[str, int], unresolved_symbols: List[str]).
        """
        target_cache = cache_path or (self.cache_dir / "universe_300_tokens.json")
        target_symbols = set(sym.strip().upper() for sym in symbols)

        # 1. Read from cache if valid and not forced
        if target_cache.exists() and not force_refresh:
            try:
                with open(target_cache, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if any(sym in cached for sym in target_symbols):
                    resolved = {k: int(v) for k, v in cached.items() if k in target_symbols and v is not None}
                    unresolved = sorted(list(target_symbols - set(resolved.keys())))
                    return resolved, unresolved
            except Exception as e:
                logger.warning(f"Failed reading token cache from {target_cache}: {e}")

        # 2. Query instrument dump from Kite client
        resolved: Dict[str, int] = {}
        if kite_client is not None:
            instruments = self.get_instruments(kite_client, exchange="NSE")
            for inst in instruments:
                sym = inst.get("tradingsymbol")
                token = inst.get("instrument_token")
                if sym in target_symbols and token:
                    resolved[sym] = int(token)

            if resolved:
                try:
                    target_cache.parent.mkdir(parents=True, exist_ok=True)
                    with open(target_cache, "w", encoding="utf-8") as f:
                        json.dump(resolved, f, indent=2)
                    logger.info(f"Cached {len(resolved)} universe tokens to {target_cache}")
                except Exception as e:
                    logger.warning(f"Error caching universe tokens: {e}")

        unresolved = sorted(list(target_symbols - set(resolved.keys())))
        return resolved, unresolved

    def resolve_lot_size(
        self,
        symbol: str = "NIFTY",
        exchange: str = "NFO",
        instrument_type: str = "FUT",
        kite_client: Optional[Any] = None,
        fallback: int = 25,
    ) -> int:
        sym_clean = symbol.strip().upper()
        cache_key = f"lotsize_{exchange}_{sym_clean}_{instrument_type}"
        if hasattr(self, "_lot_size_cache") and cache_key in self._lot_size_cache:
            return self._lot_size_cache[cache_key]
        if not hasattr(self, "_lot_size_cache"):
            self._lot_size_cache: Dict[str, int] = {}

        instruments = self.get_instruments(kite_client, exchange=exchange)
        for inst in instruments:
            name = (inst.get("name") or "").strip().upper()
            inst_type = (inst.get("instrument_type") or "").strip().upper()
            if name == sym_clean or inst.get("tradingsymbol") == sym_clean:
                if not instrument_type or inst_type == instrument_type or (instrument_type in ("CE", "PE") and inst_type in ("CE", "PE")):
                    lot = inst.get("lot_size")
                    if lot and int(lot) > 0:
                        lot_val = int(lot)
                        self._lot_size_cache[cache_key] = lot_val
                        return lot_val

        return fallback


# Global singleton instance
instrument_resolver = InstrumentResolver()
