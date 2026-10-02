"""
Authoritative Zerodha Kite Instrument Resolver.
Resolves symbols, exchanges, and expiries to numeric instrument_tokens.
Caches instrument lists locally with TTL to minimize Kite API bandwidth.
"""

from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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

UNIVERSE_TOKEN_CACHE_VERSION = 1
UNIVERSE_TOKEN_CACHE_TTL = timedelta(hours=24)


class InstrumentResolver:
    """
    Resolves symbols to numeric instrument_tokens using Kite's instrument master.
    """

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or (settings.base_dir / "data" / "cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._memory_cache: Dict[str, Dict[str, Any]] = {}
        self._memory_cache_loaded_at: Dict[str, datetime] = {}

    def _get_cache_path(self, exchange: str) -> Path:
        return self.cache_dir / f"instruments_{exchange.lower()}.json"

    def get_instruments(self, kite_client: Any, exchange: str = "NSE") -> List[Dict[str, Any]]:
        """
        Fetches instrument dump from Kite or reads from local disk cache if < 24 hours old.
        Validates that cached instruments are non-empty and well-formed.
        """
        cache_file = self._get_cache_path(exchange)

        # Check in-memory cache with 24-hour TTL and non-empty validation
        loaded_at = self._memory_cache_loaded_at.get(exchange)
        if (
            exchange in self._memory_cache
            and loaded_at is not None
            and datetime.now() - loaded_at < timedelta(hours=24)
            and len(self._memory_cache[exchange]) > 0
        ):
            return list(self._memory_cache[exchange].values())

        self._memory_cache.pop(exchange, None)
        self._memory_cache_loaded_at.pop(exchange, None)

        # Check disk cache
        if cache_file.exists():
            try:
                mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
                if datetime.now() - mtime < timedelta(hours=24):
                    with open(cache_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list) and len(data) > 0:
                        valid_items = [
                            i for i in data
                            if isinstance(i, dict)
                            and i.get("tradingsymbol")
                            and i.get("instrument_token")
                        ]
                        if len(valid_items) > 0:
                            self._memory_cache[exchange] = {i["tradingsymbol"]: i for i in valid_items}
                            self._memory_cache_loaded_at[exchange] = mtime
                            return valid_items
                        else:
                            logger.warning(
                                f"Disk cache for {exchange} contains no valid instrument records. Bypassing cache."
                            )
                    else:
                        logger.warning(
                            f"Disk cache for {exchange} is empty or invalid format. Bypassing cache."
                        )
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
                token = inst.get("instrument_token")
                tsym = inst.get("tradingsymbol")
                if token is not None and tsym:
                    try:
                        int_token = int(token)
                        if int_token > 0:
                            sanitized.append({
                                "instrument_token": int_token,
                                "tradingsymbol": str(tsym).strip().upper(),
                                "name": inst.get("name"),
                                "expiry": str(inst.get("expiry")) if inst.get("expiry") else None,
                                "strike": inst.get("strike"),
                                "lot_size": inst.get("lot_size", 1),
                                "instrument_type": inst.get("instrument_type"),
                                "segment": inst.get("segment"),
                                "exchange": exchange,
                            })
                    except (TypeError, ValueError):
                        continue

            if sanitized:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(sanitized, f)

                self._memory_cache[exchange] = {i["tradingsymbol"]: i for i in sanitized}
                self._memory_cache_loaded_at[exchange] = datetime.now()
                logger.info(f"Cached {len(sanitized)} instruments for {exchange}.")
                return sanitized
            else:
                logger.warning(f"Kite returned 0 instruments for {exchange}.")
                return []
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

    def _load_valid_universe_cache(
        self,
        cache_path: Path,
        target_symbols: Set[str],
    ) -> Optional[Dict[str, int]]:
        """
        Load the 300-stock token cache only when all integrity checks pass.

        Cache requirements:
          - correct schema/version
          - NSE exchange
          - fresh timestamp (< 24h)
          - exact target symbol set
          - positive integer tokens
          - no duplicate tokens
        """
        if not cache_path.exists():
            return None

        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            logger.warning(f"Could not read token cache {cache_path}: {e}")
            return None

        # Reject legacy/unversioned cache format.
        if not isinstance(payload, dict):
            logger.warning("Universe token cache is not a JSON object.")
            return None

        if payload.get("version") != UNIVERSE_TOKEN_CACHE_VERSION:
            logger.warning(
                "Universe token cache has unsupported/legacy schema. "
                "Forcing fresh Kite resolution."
            )
            return None

        if payload.get("exchange") != "NSE":
            logger.warning(
                f"Universe token cache exchange mismatch: "
                f"{payload.get('exchange')!r}"
            )
            return None

        generated_at_raw = payload.get("generated_at")
        if not generated_at_raw:
            logger.warning("Universe token cache has no generated_at timestamp.")
            return None

        try:
            generated_at = datetime.fromisoformat(generated_at_raw)
        except ValueError:
            logger.warning("Universe token cache has invalid generated_at timestamp.")
            return None

        if datetime.now() - generated_at >= UNIVERSE_TOKEN_CACHE_TTL:
            logger.info("Universe token cache expired; refreshing from Kite.")
            return None

        cached_symbols = {
            str(s).strip().upper()
            for s in payload.get("symbols", [])
            if s
        }

        if cached_symbols != target_symbols:
            logger.warning(
                "Universe token cache symbol set mismatch; "
                "forcing fresh Kite resolution."
            )
            return None

        raw_tokens = payload.get("tokens")
        if not isinstance(raw_tokens, dict):
            logger.warning("Universe token cache has invalid tokens payload.")
            return None

        normalized: Dict[str, int] = {}

        for symbol in target_symbols:
            raw_token = raw_tokens.get(symbol)

            if isinstance(raw_token, bool):
                logger.warning(f"Invalid boolean token for {symbol}.")
                return None

            try:
                token = int(raw_token)
            except (TypeError, ValueError):
                logger.warning(f"Invalid token for {symbol}: {raw_token!r}")
                return None

            if token <= 0:
                logger.warning(f"Non-positive token for {symbol}: {token}")
                return None

            normalized[symbol] = token

        # Critical integrity check: one NSE universe symbol must not map
        # to the same token as another symbol.
        token_to_symbols: Dict[int, List[str]] = {}

        for symbol, token in normalized.items():
            token_to_symbols.setdefault(token, []).append(symbol)

        duplicates = {
            token: symbols
            for token, symbols in token_to_symbols.items()
            if len(symbols) > 1
        }

        if duplicates:
            logger.error(
                f"Duplicate NSE instrument tokens in universe cache: {duplicates}"
            )
            return None

        return normalized

    def resolve_universe(
        self,
        symbols: List[str],
        kite_client: Optional[Any] = None,
        cache_path: Optional[Path] = None,
        force_refresh: bool = False,
    ) -> Tuple[Dict[str, int], List[str]]:
        """
        Resolve the exact 300-stock universe using the authoritative Kite NSE
        instrument master.

        Cache is used only when it is:
          - fresh
          - versioned
          - exact-match with the requested universe
          - positive/integer tokens
          - duplicate-free

        No static/hardcoded token fallback is used.
        """
        target_cache = cache_path or (
            self.cache_dir / "universe_300_tokens.json"
        )

        target_symbols: Set[str] = {
            sym.strip().upper()
            for sym in symbols
            if sym and sym.strip()
        }

        if not target_symbols:
            return {}, []

        # 1. Valid cache path.
        if not force_refresh:
            cached = self._load_valid_universe_cache(
                target_cache,
                target_symbols,
            )
            if cached is not None:
                logger.info(
                    f"Using validated universe token cache: "
                    f"{len(cached)}/{len(target_symbols)} symbols"
                )
                return cached, []

        # 2. No trustworthy cache -> authoritative Kite master.
        if kite_client is None:
            logger.error(
                "Cannot resolve universe tokens: no valid cache and no Kite client."
            )
            return {}, sorted(target_symbols)

        instruments = self.get_instruments(
            kite_client,
            exchange="NSE",
        )

        resolved: Dict[str, int] = {}
        token_to_symbols: Dict[int, List[str]] = {}

        for inst in instruments:
            symbol = str(inst.get("tradingsymbol") or "").strip().upper()
            raw_token = inst.get("instrument_token")

            if symbol not in target_symbols:
                continue

            try:
                token = int(raw_token)
            except (TypeError, ValueError):
                logger.warning(
                    f"Ignoring invalid Kite token for {symbol}: {raw_token!r}"
                )
                continue

            if token <= 0:
                logger.warning(
                    f"Ignoring non-positive Kite token for {symbol}: {token}"
                )
                continue

            resolved[symbol] = token
            token_to_symbols.setdefault(token, []).append(symbol)

        # 3. Reject duplicate token assignments.
        duplicate_tokens = {
            token: sorted(set(symbols_for_token))
            for token, symbols_for_token in token_to_symbols.items()
            if len(set(symbols_for_token)) > 1
        }

        if duplicate_tokens:
            logger.error(
                f"Duplicate instrument tokens returned for NSE universe: "
                f"{duplicate_tokens}"
            )

            for token, duplicate_symbols in duplicate_tokens.items():
                for symbol in duplicate_symbols:
                    resolved.pop(symbol, None)

        unresolved = sorted(target_symbols - set(resolved))

        # 4. Cache ONLY a complete, duplicate-free universe mapping.
        if not unresolved and len(resolved) == len(target_symbols):
            payload = {
                "version": UNIVERSE_TOKEN_CACHE_VERSION,
                "exchange": "NSE",
                "generated_at": datetime.now().isoformat(),
                "symbols": sorted(target_symbols),
                "tokens": {
                    symbol: resolved[symbol]
                    for symbol in sorted(target_symbols)
                },
            }

            try:
                target_cache.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                with open(target_cache, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)

                logger.info(
                    f"Cached validated NSE universe tokens: "
                    f"{len(resolved)}/{len(target_symbols)}"
                )

            except Exception as e:
                logger.warning(
                    f"Could not write universe token cache: {e}"
                )
        else:
            logger.error(
                f"NSE universe token resolution incomplete: "
                f"{len(resolved)}/{len(target_symbols)} resolved. "
                f"Cache will NOT be written."
            )

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
