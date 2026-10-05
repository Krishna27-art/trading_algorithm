"""
Authoritative Zerodha Kite Instrument Resolver.
Resolves symbols, exchanges, and expiries to numeric instrument_tokens.
Caches instrument lists locally with TTL to minimize Kite API bandwidth.
"""

from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from config.settings import settings
from data.time_utils import IST, now_ist_naive

logger = logging.getLogger(__name__)

INDEX_SYMBOL_ALIASES = {
    "NIFTY": "NIFTY 50",
    "NIFTY 50": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "NIFTY BANK": "NIFTY BANK",
    "FINNIFTY": "NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NIFTY MID SELECT",
    "INDIA VIX": "INDIA VIX",
    "INDIAVIX": "INDIA VIX",
}

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
            and now_ist_naive() - loaded_at < timedelta(hours=24)
            and len(self._memory_cache[exchange]) > 0
        ):
            return list(self._memory_cache[exchange].values())

        self._memory_cache.pop(exchange, None)
        self._memory_cache_loaded_at.pop(exchange, None)

        min_expected = 500 if exchange == "NSE" else 1

        # Check disk cache
        if cache_file.exists():
            try:
                cache_age_seconds = time.time() - cache_file.stat().st_mtime
                if cache_age_seconds < UNIVERSE_TOKEN_CACHE_TTL.total_seconds():
                    with open(cache_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list) and len(data) >= min_expected:
                        valid_items = []
                        for item in data:
                            if not isinstance(item, dict):
                                continue
                            tradingsymbol = str(
                                item.get("tradingsymbol") or ""
                            ).strip().upper()
                            raw_token = item.get("instrument_token")
                            if not tradingsymbol:
                                continue
                            if isinstance(raw_token, bool):
                                continue
                            try:
                                token = int(raw_token)
                            except (TypeError, ValueError):
                                continue
                            if token <= 0:
                                continue
                            valid_items.append({
                                **item,
                                "tradingsymbol": tradingsymbol,
                                "instrument_token": token,
                            })

                        if len(valid_items) >= min_expected:
                            self._memory_cache[exchange] = {i["tradingsymbol"]: i for i in valid_items}
                            self._memory_cache_loaded_at[exchange] = (
                                datetime.fromtimestamp(
                                    cache_file.stat().st_mtime,
                                    IST,
                                ).replace(tzinfo=None)
                            )
                            return valid_items
                        else:
                            logger.warning(
                                f"Disk cache for {exchange} contains only {len(valid_items)} valid records (< {min_expected}). Bypassing cache."
                            )
                    else:
                        logger.warning(
                            f"Disk cache for {exchange} is incomplete or invalid format (contains {len(data) if isinstance(data, list) else 'invalid'} items). Bypassing cache."
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
                if len(sanitized) >= min_expected:
                    with open(cache_file, "w", encoding="utf-8") as f:
                        json.dump(sanitized, f)
                    logger.info(f"Cached {len(sanitized)} instruments for {exchange}.")
                else:
                    logger.info(
                        f"Fetched {len(sanitized)} instruments for {exchange} (in-memory only; below minimum threshold of {min_expected})."
                    )

                self._memory_cache[exchange] = {i["tradingsymbol"]: i for i in sanitized}
                self._memory_cache_loaded_at[exchange] = now_ist_naive()
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

        if exchange == "NSE" and sym_clean in CANONICAL_INDEX_TOKENS:
            return CANONICAL_INDEX_TOKENS[sym_clean]

        lookup_symbol = INDEX_SYMBOL_ALIASES.get(
            sym_clean,
            sym_clean,
        )

        instruments = self.get_instruments(
            kite_client,
            exchange=exchange,
        )

        lookup_candidates = {
            lookup_symbol,
            f"{lookup_symbol}-BE",
            lookup_symbol.replace("-BE", ""),
        }

        for inst in instruments:
            tradingsymbol = str(
                inst.get("tradingsymbol") or ""
            ).strip().upper()

            if tradingsymbol not in lookup_candidates:
                continue

            raw_token = inst.get("instrument_token")

            try:
                token = int(raw_token)
            except (TypeError, ValueError):
                continue

            if token <= 0:
                continue

            return token

        if exchange == "NFO" and "NIFTY" in sym_clean:
            today = now_ist_naive().date()
            fut_candidates = []
            for inst in instruments:
                if (
                    str(inst.get("name") or "").strip().upper() != "NIFTY"
                    or str(inst.get("instrument_type") or "").strip().upper() != "FUT"
                ):
                    continue

                expiry_raw = str(inst.get("expiry") or "")[:10]
                try:
                    expiry = datetime.fromisoformat(expiry_raw).date()
                except ValueError:
                    continue

                if expiry < today:
                    continue

                try:
                    token = int(inst["instrument_token"])
                except (TypeError, ValueError, KeyError):
                    continue

                if token <= 0:
                    continue

                fut_candidates.append((expiry, token))

            if fut_candidates:
                fut_candidates.sort(key=lambda item: item[0])
                return fut_candidates[0][1]

        return None

    def _load_valid_universe_cache(
        self,
        cache_path: Path,
        target_symbols: Set[str],
    ) -> Optional[Dict[str, int]]:
        """
        Load the 700-stock token cache only when all integrity checks pass.

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

        if now_ist_naive() - generated_at >= UNIVERSE_TOKEN_CACHE_TTL:
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
        Resolve the exact 700-stock universe using the authoritative Kite NSE
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
            self.cache_dir / "universe_700_tokens.json"
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
            # If target_cache exists and has all target symbols, return it as offline fallback
            if target_cache.exists():
                try:
                    with open(target_cache, "r", encoding="utf-8") as f:
                        payload = json.load(f)
                    raw_tokens = payload.get("tokens", {})
                    if isinstance(raw_tokens, dict) and target_symbols.issubset(raw_tokens.keys()):
                        fallback_tokens = {
                            s: int(raw_tokens[s])
                            for s in target_symbols
                            if int(raw_tokens[s]) > 0
                        }
                        if len(fallback_tokens) == len(target_symbols):
                            logger.info(
                                f"Using existing universe token cache as offline fallback: {len(fallback_tokens)} symbols."
                            )
                            return fallback_tokens, []
                except Exception:
                    pass
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

        # Build lookup table of valid NSE instrument tokens
        inst_by_sym: Dict[str, int] = {}
        for inst in instruments:
            symbol = str(inst.get("tradingsymbol") or "").strip().upper()
            raw_token = inst.get("instrument_token")
            try:
                token = int(raw_token)
                if token > 0:
                    inst_by_sym[symbol] = token
            except (TypeError, ValueError):
                continue

        for target in target_symbols:
            # 1. Exact match
            # 2. -BE series fallback (e.g. TNTELE -> TNTELE-BE)
            # 3. Strip -BE fallback (e.g. HFCL-BE -> HFCL)
            token = (
                inst_by_sym.get(target)
                or inst_by_sym.get(f"{target}-BE")
                or inst_by_sym.get(target.replace("-BE", ""))
            )
            if token is not None:
                resolved[target] = token
                token_to_symbols.setdefault(token, []).append(target)

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
                "generated_at": now_ist_naive().isoformat(),
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
        fallback: Optional[int] = None,
    ) -> Optional[int]:
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

        if fallback is not None:
            logger.warning(
                "Using explicitly supplied lot-size fallback for %s:%s",
                exchange,
                sym_clean,
            )
            return fallback

        logger.error(
            "Could not resolve lot size for %s:%s:%s from Kite instrument master.",
            exchange,
            sym_clean,
            instrument_type,
        )
        return None

    def find_nearest_single_stock_future(
        self,
        underlying_symbol: str,
        instruments: Optional[List[Dict[str, Any]]] = None,
        kite_client: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Return the nearest non-expired single-stock futures contract.

        Uses the real NFO instrument dump and expiry date. No hardcoded contract
        symbols or tokens are used.
        """
        clean = str(underlying_symbol).strip().upper()

        if instruments is None:
            instruments = self.get_instruments(
                kite_client,
                exchange="NFO",
            )

        today = now_ist_naive().date()
        candidates = []

        for inst in instruments:
            if not isinstance(inst, dict):
                continue

            if str(inst.get("instrument_type", "")).upper() != "FUT":
                continue

            segment = str(inst.get("segment", "")).upper()
            if segment and segment != "NFO-FUT":
                continue

            name = str(inst.get("name", "")).strip().upper()
            tradingsymbol = str(inst.get("tradingsymbol", "")).strip().upper()

            if name != clean and not tradingsymbol.startswith(clean):
                continue

            expiry_raw = str(inst.get("expiry", ""))[:10]
            if not expiry_raw:
                continue

            try:
                expiry = datetime.fromisoformat(expiry_raw).date()
            except ValueError:
                continue

            if expiry < today:
                continue

            try:
                token = int(inst["instrument_token"])
            except (TypeError, ValueError, KeyError):
                continue

            if token <= 0:
                continue

            candidates.append((
                expiry,
                {
                    "instrument_token": token,
                    "tradingsymbol": tradingsymbol,
                    "expiry": expiry.isoformat(),
                    "name": name,
                },
            ))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]


# Global singleton instance
instrument_resolver = InstrumentResolver()
