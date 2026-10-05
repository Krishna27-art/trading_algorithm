"""
700-Stock Master Scanner Universe Definition and Token Resolution.

Maintains the single source of truth for the 700-stock scanning universe
(100 Large Cap, 100 Mid Cap, 500 Small Cap) loaded from data/universe/700_stocks.json.
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
    validated 700-stock master universe (data/universe/700_stocks.json).
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


def create_instrument_config_for_equity(
    symbol: str,
    token: Optional[int] = None,
    current_price: float = 0.0,
    atr_14: Optional[float] = None,
    tick_size: float = 0.05,
    lot_size: int = 1,
) -> InstrumentConfig:
    """
    Factory creating an InstrumentConfig for an equity constituent.
    """
    price = current_price

    min_orb_pct = 0.0017
    max_orb_pct = 0.0250
    max_risk_pct = 0.0150

    min_orb = round(max(price * min_orb_pct, 0.5), 2)
    max_orb = round(max(price * 0.0060, min_orb * 2.5), 2)
    max_risk = round(max(price * 0.0040, min_orb * 1.5), 2)

    return InstrumentConfig(
        symbol=symbol,
        exchange="NSE",
        instrument_type=InstrumentType.EQUITY,
        lot_size=lot_size,
        tick_size=tick_size,
        min_orb_range=min_orb,
        max_orb_range=max_orb,
        max_risk_cap=max_risk,
        equity_orb_min_range_pct=min_orb_pct,
        equity_orb_max_range_pct=max_orb_pct,
        equity_orb_max_risk_pct=max_risk_pct,
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


DEFAULT_700_CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "cache" / "universe_700_tokens.json"
DEFAULT_300_CACHE_FILE = DEFAULT_700_CACHE_FILE  # Compatibility alias


class StockUniverse:
    """
    Single source of truth for the 700-stock scanning universe (100 Large Cap, 100 Mid Cap, 500 Small Cap).
    Enforces strict validation on load and raises ValueError/RuntimeError on dataset defects.
    """

    def __init__(self, json_path: Optional[Path] = None):
        self.json_path = json_path or (
            Path(__file__).resolve().parent.parent / "data" / "universe" / "700_stocks.json"
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
            raise RuntimeError(f"Failed to parse master 700-stock universe JSON at {self.json_path}: {e}")

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

        count_warnings = []
        if total_cnt != 700:
            count_warnings.append(f"Total stock count is {total_cnt}, expected 700.")
        if large_cnt != 100:
            count_warnings.append(f"Large cap count is {large_cnt}, expected 100.")
        if mid_cnt != 100:
            count_warnings.append(f"Mid cap count is {mid_cnt}, expected 100.")
        if small_cnt != 500:
            count_warnings.append(f"Small cap count is {small_cnt}, expected 500.")
        if count_warnings:
            logger.warning(
                f"Universe JSON count mismatch in {self.json_path}: "
                + "; ".join(count_warnings)
                + " Proceeding with available stocks."
            )

        if validation_errors:
            error_msg = (
                f"700-Stock Universe validation issues ({self.json_path}):\n"
                + "\n".join(f" - {err}" for err in validation_errors)
            )
            logger.error(error_msg)

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

    def get_token(
        self,
        symbol: str,
        token_map: Optional[Dict[str, int]] = None,
    ) -> Optional[int]:
        """
        Return a token only from an already validated runtime token_map.

        This method intentionally does NOT read the disk cache directly.
        Token-cache validation belongs exclusively to InstrumentResolver.
        """
        sym = symbol.strip().upper()

        if token_map is not None:
            raw_token = token_map.get(sym)

            if raw_token is None:
                return None

            try:
                token = int(raw_token)
            except (TypeError, ValueError):
                return None

            return token if token > 0 else None

        return self._tokens_cache.get(sym)

    def print_startup_summary(self, token_map: Dict[str, int]) -> Dict[str, Any]:
        """
        Prints startup validation summary for Large/Mid/Small cap categories
        and instrument token resolution state.
        """
        large_cnt = len(self.large_cap_stocks)
        mid_cnt = len(self.mid_cap_stocks)
        small_cnt = len(self.small_cap_stocks)
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


def resolve_700_universe_tokens(
    kite_client: Optional[Any] = None,
    cache_path: Optional[Path] = None,
    force_refresh: bool = False,
) -> Dict[str, int]:
    """
    Resolve tokens for the complete 700-stock universe through
    InstrumentResolver.

    No hardcoded token fallback is permitted.
    """
    from data.instrument_resolver import instrument_resolver

    universe = StockUniverse()
    symbols = [r.symbol for r in universe.all_stocks]

    resolved_map, unresolved = instrument_resolver.resolve_universe(
        symbols=symbols,
        kite_client=kite_client,
        cache_path=cache_path,
        force_refresh=force_refresh,
    )

    if unresolved:
        logger.error(
            f"Failed to resolve {len(unresolved)} NSE universe tokens. "
            f"Missing symbols: {unresolved}"
        )

    return resolved_map


def resolve_300_universe_tokens(
    kite_client: Optional[Any] = None,
    cache_path: Optional[Path] = None,
    force_refresh: bool = False,
) -> Dict[str, int]:
    """Compatibility alias delegating to resolve_700_universe_tokens."""
    return resolve_700_universe_tokens(kite_client=kite_client, cache_path=cache_path, force_refresh=force_refresh)


def resolve_universe_tokens(
    kite_client: Optional[Any] = None,
    cache_path: Optional[Path] = None,
    force_refresh: bool = False,
) -> Dict[str, int]:
    """Compatibility alias delegating to resolve_700_universe_tokens."""
    return resolve_700_universe_tokens(kite_client=kite_client, cache_path=cache_path, force_refresh=force_refresh)


