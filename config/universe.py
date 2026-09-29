"""
300-Stock Master Scanner Universe Definition and Token Resolution.

Maintains the single source of truth for the 300-stock scanning universe
(100 Large Cap, 100 Mid Cap, 100 Small Cap) loaded from data/universe/300_stocks.json.

REMOVED vs. the original: the hardcoded NIFTY_50_CONSTITUENTS and
NIFTY_100_CONSTITUENTS lists. They were dead weight — the file's own
comments called them "kept for historical reference/testing", and the only
non-test reference was get_universe()'s fallback below. data/universe/300_stocks.json
(loaded by StockUniverse) is the actual, validated single source of truth
for the scanning universe; these hardcoded lists duplicated a subset of it
and could silently drift out of sync.

Note: tests/test_scanner.py asserts against NIFTY_50_CONSTITUENTS directly
and will need its own update (inline the ~3 symbols it checks, or assert
against StockUniverse().large_cap_100 instead) — that file is outside the
scope of this change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import InstrumentConfig, InstrumentType, settings
from monitoring.logger import logger


def get_universe(
    as_of: Optional[date] = None,
    membership_csv: Optional[Path] = None,
) -> List[str]:
    """
    Point-in-time accessor for a ~100-stock reference universe (kept for
    historical index analysis). Prefers a dated membership CSV when one is
    supplied; otherwise falls back to the large-cap 100 slice of the
    validated 300-stock master universe (data/universe/300_stocks.json).
    """
    csv_path = membership_csv or (settings.base_dir / "data" / "cache" / "nifty100_membership.csv")

    if csv_path.exists() and as_of is not None:
        try:
            import pandas as pd
            df = pd.read_csv(csv_path, parse_dates=["date"])
            df["date"] = pd.to_datetime(df["date"]).dt.date
            valid_dates = df[df["date"] <= as_of]["date"]
            if not valid_dates.empty:
                target_date = valid_dates.max()
                symbols = df[df["date"] == target_date]["symbol"].dropna().unique().tolist()
                if symbols:
                    return sorted(symbols)
        except Exception as e:
            logger.warning(f"Failed to read membership history from {csv_path}: {e}")

    fallback = sorted(r.symbol for r in StockUniverse().large_cap_100)
    return fallback


# Static reference tokens for offline fallback
_FALLBACK_NSE_TOKENS: Dict[str, int] = {
    "ADANIENT": 6401,
    "ADANIPORTS": 3861249,
    "APOLLOHOSP": 40193,
    "ASIANPAINT": 60417,
    "AXISBANK": 1510401,
    "BAJAJ-AUTO": 4267265,
    "BAJFINANCE": 81153,
    "BAJAJFINSV": 4268801,
    "BEL": 98049,
    "BPCL": 134657,
    "BHARTIARTL": 2714625,
    "BRITANNIA": 140033,
    "CIPLA": 177665,
    "COALINDIA": 5215745,
    "DRREDDY": 225537,
    "EICHERMOT": 232961,
    "GRASIM": 315393,
    "HCLTECH": 1850625,
    "HDFCBANK": 341249,
    "HDFCLIFE": 119553,
    "HEROMOTOCO": 345089,
    "HINDALCO": 348929,
    "HINDUNILVR": 356865,
    "ICICIBANK": 1270529,
    "ITC": 424961,
    "INDUSINDBK": 1346049,
    "INFY": 408065,
    "JSWSTEEL": 3001089,
    "KOTAKBANK": 492033,
    "LT": 2939649,
    "M&M": 519937,
    "MARUTI": 2815745,
    "NESTLEIND": 4598529,
    "NTPC": 2977281,
    "ONGC": 633601,
    "POWERGRID": 3834113,
    "RELIANCE": 738561,
    "SBILIFE": 5582849,
    "SHRIRAMFIN": 1102337,
    "SBIN": 779521,
    "SUNPHARMA": 857857,
    "TCS": 2953217,
    "TATACONSUM": 878593,
    "TATAMOTORS": 884737,
    "TMPV": 884737,
    "ETERNAL": 1304833,
    "ASHOKLEY": 54273,
    "TATASTEEL": 895745,
    "TECHM": 3465729,
    "TITAN": 897537,
    "TRENT": 5048577,
    "ULTRACEMCO": 2952193,
    "WIPRO": 969473,
}


def create_instrument_config_for_equity(
    symbol: str,
    token: Optional[int] = None,
    current_price: float = 1000.0,
    atr_14: Optional[float] = None,
) -> InstrumentConfig:
    """
    Factory creating an InstrumentConfig for an equity constituent.
    """
    price = max(current_price, 10.0)

    min_orb = round(max(price * 0.0017, 0.5), 2)
    max_orb = round(max(price * 0.0060, min_orb * 2.5), 2)
    max_risk = round(max(price * 0.0040, min_orb * 1.5), 2)

    return InstrumentConfig(
        symbol=symbol,
        exchange="NSE",
        instrument_type=InstrumentType.EQUITY,
        lot_size=1,
        tick_size=0.05,
        min_orb_range=min_orb,
        max_orb_range=max_orb,
        max_risk_cap=max_risk,
        instrument_token=token,
    )


@dataclass
class StockRecord:
    symbol: str
    name: str
    market_cap_rank: int
    category: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "market_cap_rank": self.market_cap_rank,
            "category": self.category,
        }


DEFAULT_300_CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "cache" / "universe_300_tokens.json"


class StockUniverse:
    """
    Single source of truth for the 300-stock scanning universe (100 Large Cap, 100 Mid Cap, 100 Small Cap).
    Enforces strict validation on load and raises ValueError/RuntimeError on dataset defects.
    """

    def __init__(self, json_path: Optional[Path] = None):
        self.json_path = json_path or (
            Path(__file__).resolve().parent.parent / "data" / "universe" / "300_stocks.json"
        )
        self._records: List[StockRecord] = []
        self._symbol_map: Dict[str, StockRecord] = {}
        self._tokens_cache: Dict[str, int] = {}
        self._load_and_validate()

    def _load_and_validate(self) -> None:
        if not self.json_path.exists():
            raise FileNotFoundError(f"Master universe data dataset file not found: {self.json_path}")

        try:
            with open(self.json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise RuntimeError(f"Failed to parse master 300-stock universe JSON at {self.json_path}: {e}")

        if not isinstance(data, list):
            raise ValueError(f"Master universe file {self.json_path} must contain a JSON array of stock objects.")

        records: List[StockRecord] = []
        validation_errors: List[str] = []
        seen_symbols = set()

        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                validation_errors.append(f"Item at index {idx} is not a JSON object.")
                continue

            sym = str(item.get("symbol", "")).strip().upper()
            name = str(item.get("name", "")).strip()
            rank = item.get("market_cap_rank")
            cat = str(item.get("category", "")).strip().lower()

            is_duplicate = False
            if not sym:
                validation_errors.append(f"Item at index {idx} missing required field 'symbol'.")
            elif sym in seen_symbols:
                validation_errors.append(f"Duplicate stock symbol '{sym}' found at index {idx}.")
                is_duplicate = True
            else:
                seen_symbols.add(sym)

            if is_duplicate:
                continue  # Skip duplicate — keep only the first occurrence

            if not name:
                validation_errors.append(f"Stock '{sym or idx}' missing required field 'name'.")

            try:
                rank_int = int(rank)
                if rank_int <= 0:
                    validation_errors.append(f"Stock '{sym}' invalid 'market_cap_rank': {rank}.")
            except (ValueError, TypeError):
                validation_errors.append(f"Stock '{sym}' non-integer 'market_cap_rank': {rank}.")
                rank_int = 0

            if cat not in ("large", "mid", "small"):
                validation_errors.append(f"Stock '{sym}' invalid 'category': '{cat}'. Must be 'large', 'mid', or 'small'.")

            records.append(StockRecord(symbol=sym, name=name, market_cap_rank=rank_int, category=cat))


        large_cnt = sum(1 for r in records if r.category == "large")
        mid_cnt = sum(1 for r in records if r.category == "mid")
        small_cnt = sum(1 for r in records if r.category == "small")
        total_cnt = len(records)

        # Fix 3.4: Soft-degrade on count mismatch rather than hard-crashing.
        # Log warnings for any category that is off, but proceed with whatever
        # stocks are actually in the file so the scanner remains usable.
        count_warnings = []
        if total_cnt != 300:
            count_warnings.append(f"Total stock count is {total_cnt}, expected 300.")
        if large_cnt != 100:
            count_warnings.append(f"Large cap count is {large_cnt}, expected 100.")
        if mid_cnt != 100:
            count_warnings.append(f"Mid cap count is {mid_cnt}, expected 100.")
        if small_cnt != 100:
            count_warnings.append(f"Small cap count is {small_cnt}, expected 100.")
        if count_warnings:
            logger.warning(
                f"Universe JSON count mismatch in {self.json_path}: "
                + "; ".join(count_warnings)
                + " Proceeding with available stocks."
            )

        if validation_errors:
            # Symbol/name/rank errors are harder failures — log them but still
            # proceed with the valid records; don't crash the whole backend.
            error_msg = (
                f"300-Stock Universe validation issues ({self.json_path}):\n"
                + "\n".join(f" - {err}" for err in validation_errors)
            )
            logger.error(error_msg)

        # Only keep records with a valid (non-empty) symbol so the scanner
        # doesn't receive incomplete entries even in partial-degrade mode.
        records = [r for r in records if r.symbol]

        self._records = records
        self._symbol_map = {r.symbol: r for r in records}


    @property
    def large_cap_stocks(self) -> List[StockRecord]:
        return [r for r in self._records if r.category == "large"]

    @property
    def mid_cap_stocks(self) -> List[StockRecord]:
        return [r for r in self._records if r.category == "mid"]

    @property
    def small_cap_stocks(self) -> List[StockRecord]:
        return [r for r in self._records if r.category == "small"]

    @property
    def large_cap_100(self) -> List[StockRecord]:
        return self.large_cap_stocks

    @property
    def mid_cap_100(self) -> List[StockRecord]:
        return self.mid_cap_stocks

    @property
    def small_cap_100(self) -> List[StockRecord]:
        return self.small_cap_stocks

    @property
    def all_stocks(self) -> List[StockRecord]:
        return list(self._records)

    @property
    def all_symbols(self) -> List[str]:
        return [r.symbol for r in self._records]

    def get_stock(self, symbol: str) -> Optional[StockRecord]:
        """Lookup StockRecord by tradingsymbol."""
        return self._symbol_map.get(symbol.strip().upper())

    def get_token(self, symbol: str, token_map: Optional[Dict[str, int]] = None) -> Optional[int]:
        """Lookup numeric Kite instrument token by tradingsymbol."""
        sym = symbol.strip().upper()
        if token_map and sym in token_map:
            return token_map[sym]
        if sym in self._tokens_cache:
            return self._tokens_cache[sym]
        if not self._tokens_cache and DEFAULT_300_CACHE_FILE.exists():
            try:
                with open(DEFAULT_300_CACHE_FILE, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                    self._tokens_cache = {k.strip().upper(): int(v) for k, v in cached.items() if v is not None}
                return self._tokens_cache.get(sym)
            except Exception:
                pass
        return None

    def print_startup_summary(self, token_map: Dict[str, int]) -> Dict[str, Any]:
        """
        Prints startup validation summary for Large/Mid/Small cap categories
        and instrument token resolution state.
        """
        large_cnt = len(self.large_cap_100)
        mid_cnt = len(self.mid_cap_100)
        small_cnt = len(self.small_cap_100)
        total_cnt = len(self.all_stocks)

        self._tokens_cache.update({k: v for k, v in token_map.items() if v is not None})

        resolved_syms = [
            r.symbol for r in self.all_stocks if r.symbol in token_map and token_map[r.symbol] is not None
        ]
        missing_syms = [
            r.symbol for r in self.all_stocks if r.symbol not in token_map or token_map[r.symbol] is None
        ]
        resolved_count = len(resolved_syms)
        missing_count = len(missing_syms)

        startup_str = (
            f"\nUniverse:\n"
            f"Large Cap: {large_cnt}\n"
            f"Mid Cap: {mid_cnt}\n"
            f"Small Cap: {small_cnt}\n"
            f"Total: {total_cnt}\n\n"
            f"Resolved: {resolved_count}/{total_cnt}\n"
            f"Missing: {missing_count}"
        )
        print(startup_str)
        logger.info(startup_str)

        if missing_count > 0:
            logger.warning(f"Unresolved universe symbols ({missing_count}): {missing_syms}")

        return {
            "large_cap_count": large_cnt,
            "mid_cap_count": mid_cnt,
            "small_cap_count": small_cnt,
            "total_count": total_cnt,
            "resolved_count": resolved_count,
            "missing_count": missing_count,
            "missing_symbols": missing_syms,
        }


def resolve_300_universe_tokens(
    kite_client: Optional[Any] = None,
    cache_path: Optional[Path] = None,
    force_refresh: bool = False,
) -> Dict[str, int]:
    """
    Resolves Kite instrument tokens for all 300 stocks in the scanning universe
    using instrument_resolver.
    """
    from data.instrument_resolver import instrument_resolver

    universe = StockUniverse()
    symbols = [r.symbol for r in universe.all_stocks]
    resolved_map, _ = instrument_resolver.resolve_universe(
        symbols=symbols,
        kite_client=kite_client,
        cache_path=cache_path,
        force_refresh=force_refresh,
    )

    # Prioritize dynamic resolved tokens; use _FALLBACK_NSE_TOKENS only for missing entries
    merged = dict(resolved_map)
    for sym, tok in _FALLBACK_NSE_TOKENS.items():
        if sym not in merged:
            logger.debug(f"Using static fallback token {tok} for {sym}")
            merged[sym] = tok
    return merged


def resolve_universe_tokens(
    kite_client: Optional[Any] = None,
    cache_path: Optional[Path] = None,
    force_refresh: bool = False,
) -> Dict[str, int]:
    """Compatibility alias delegating to resolve_300_universe_tokens."""
    return resolve_300_universe_tokens(kite_client=kite_client, cache_path=cache_path, force_refresh=force_refresh)
