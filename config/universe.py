"""
NIFTY 50 Universe Definition and Token Resolution.

IMPORTANT REBALANCING NOTICE:
-----------------------------
NSE Indices rebalances the NIFTY 50 index roughly twice a year (semi-annually,
typically announced in February/August and effective in March/September).
This list reflects the official NIFTY 50 constituents as maintained by NSE.
Periodically review and manually refresh this constituent list against NSE's
published constituent factsheet (available via nseindia.com). Do NOT dynamically
scrape nseindia.com during runtime: NSE enforces stringent anti-scraping and
Cloudflare bot detection, and static deterministic universe definitions are
essential for predictable, production-grade algorithmic execution.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import InstrumentConfig, InstrumentType, settings
from monitoring.logger import logger

# Official NSE Tradingsymbols for current NIFTY 50 constituents (Alphabetical)
NIFTY_50_CONSTITUENTS: List[str] = [
    "ADANIENT",
    "ADANIPORTS",
    "APOLLOHOSP",
    "ASIANPAINT",
    "AXISBANK",
    "BAJAJ-AUTO",
    "BAJFINANCE",
    "BAJAJFINSV",
    "BEL",
    "BPCL",
    "BHARTIARTL",
    "BRITANNIA",
    "CIPLA",
    "COALINDIA",
    "DRREDDY",
    "EICHERMOT",
    "GRASIM",
    "HCLTECH",
    "HDFCBANK",
    "HDFCLIFE",
    "HEROMOTOCO",
    "HINDALCO",
    "HINDUNILVR",
    "ICICIBANK",
    "ITC",
    "INDUSINDBK",
    "INFY",
    "JSWSTEEL",
    "KOTAKBANK",
    "LT",
    "M&M",
    "MARUTI",
    "NESTLEIND",
    "NTPC",
    "ONGC",
    "POWERGRID",
    "RELIANCE",
    "SBILIFE",
    "SHRIRAMFIN",
    "SBIN",
    "SUNPHARMA",
    "TCS",
    "TATACONSUM",
    "TATAMOTORS",
    "TATASTEEL",
    "TECHM",
    "TITAN",
    "TRENT",
    "ULTRACEMCO",
    "WIPRO",
]

# Official NSE Tradingsymbols for current NIFTY 100 constituents (Alphabetical, 100 names)
NIFTY_100_CONSTITUENTS: List[str] = sorted(list(set(NIFTY_50_CONSTITUENTS + [
    "ABB",
    "ADANIENSOL",
    "ADANIGREEN",
    "ADANIPOWER",
    "AMBUJACEM",
    "ATGL",
    "BAJAJHLDNG",
    "BANKBARODA",
    "BHEL",
    "BOSCHLTD",
    "CANBK",
    "CHOLAFIN",
    "COFORGE",
    "COLPAL",
    "DABUR",
    "DIVISLAB",
    "DLF",
    "GAIL",
    "GODREJCP",
    "HAL",
    "HAVELLS",
    "ICICIGI",
    "ICICIPRULI",
    "INDIANB",
    "INDHOTEL",
    "INDIGO",
    "IOC",
    "IRCTC",
    "IRFC",
    "JIOFIN",
    "JSWENERGY",
    "JSWINFRA",
    "LICI",
    "LTIM",
    "MAXHEALTH",
    "MOTHERSON",
    "NAUKRI",
    "NHPC",
    "PFC",
    "PIDILITIND",
    "PNB",
    "RECLTD",
    "SIEMENS",
    "TVSMOTOR",
    "TATAPOWER",
    "TORNTPHARM",
    "VBL",
    "VEDL",
    "ZOMATO",
    "ZYDUSLIFE",
])))


def get_universe(
    as_of: Optional[date] = None,
    membership_csv: Optional[Path] = None,
) -> List[str]:
    """
    Point-in-time accessor for NIFTY 100 constituents.
    If membership_csv is provided or exists at data/cache/nifty100_membership.csv (format: date,symbol),
    it reads the point-in-time constituent list for as_of.
    Otherwise, falls back to the static list with an explicit survivorship warning.
    """
    csv_path = membership_csv or (settings.base_dir / "data" / "cache" / "nifty100_membership.csv")

    if csv_path.exists() and as_of is not None:
        try:
            import pandas as pd
            df = pd.read_csv(csv_path, parse_dates=["date"])
            df["date"] = pd.to_datetime(df["date"]).dt.date
            # Filter for latest date <= as_of
            valid_dates = df[df["date"] <= as_of]["date"]
            if not valid_dates.empty:
                target_date = valid_dates.max()
                symbols = df[df["date"] == target_date]["symbol"].dropna().unique().tolist()
                if symbols:
                    return sorted(symbols)
        except Exception as e:
            logger.warning(f"Failed to read membership history from {csv_path}: {e}")

    logger.warning("Using static NIFTY 100 universe; results carry survivorship bias.")
    return list(NIFTY_100_CONSTITUENTS)


# Static reference tokens for offline / fallback execution without live Kite instruments dump
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
    "TATASTEEL": 895745,
    "TECHM": 3465729,
    "TITAN": 897537,
    "TRENT": 5048577,
    "ULTRACEMCO": 2952193,
    "WIPRO": 969473,
}

DEFAULT_CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "cache" / "nifty50_tokens.json"


def resolve_universe_tokens(
    kite_client: Optional[Any] = None,
    cache_path: Optional[Path] = None,
    force_refresh: bool = False,
) -> Dict[str, int]:
    """
    Resolves Kite instrument tokens for all NIFTY 50 constituents.
    1. Checks if local JSON cache exists.
    2. If missing or force_refresh=True, downloads the NSE instrument dump once
       via kite.instruments("NSE") and parses tokens in a single O(N) pass.
    3. Falls back gracefully to static tokens if offline.
    """
    target_cache = cache_path or DEFAULT_CACHE_FILE

    # 1. Read from cache if valid
    if target_cache.exists() and not force_refresh:
        try:
            with open(target_cache, "r", encoding="utf-8") as f:
                data = json.load(f)
            if all(sym in data for sym in NIFTY_50_CONSTITUENTS):
                logger.info(f"Loaded {len(data)} NIFTY 50 tokens from cache: {target_cache}")
                return data
        except Exception as e:
            logger.warning(f"Error reading token cache from {target_cache}: {e}")

    # 2. Query live Kite if client provided
    resolved: Dict[str, int] = {}
    if kite_client is not None:
        try:
            # kite_client can be KiteApp or KiteConnect
            client = getattr(kite_client, "kite", kite_client)
            if hasattr(client, "instruments"):
                logger.info("Resolving NIFTY 50 instrument tokens via kite.instruments('NSE')...")
                all_nse = client.instruments("NSE")
                target_set = set(NIFTY_50_CONSTITUENTS)
                for inst in all_nse:
                    sym = inst.get("tradingsymbol")
                    if sym in target_set:
                        resolved[sym] = int(inst["instrument_token"])

                if len(resolved) == len(NIFTY_50_CONSTITUENTS):
                    target_cache.parent.mkdir(parents=True, exist_ok=True)
                    with open(target_cache, "w", encoding="utf-8") as f:
                        json.dump(resolved, f, indent=2)
                    logger.info(f"Resolved and cached {len(resolved)} NIFTY 50 tokens to {target_cache}")
                    return resolved
                else:
                    missing = target_set - set(resolved.keys())
                    logger.warning(f"Kite dump missing tokens for: {missing}. Merging with fallback.")
        except Exception as e:
            logger.warning(f"Failed to fetch live instrument dump from Kite: {e}")

    # 3. Fallback to bundled static mapping
    merged = dict(_FALLBACK_NSE_TOKENS)
    merged.update(resolved)

    # Save merged fallback to cache if not already present
    try:
        target_cache.parent.mkdir(parents=True, exist_ok=True)
        with open(target_cache, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
    except Exception:
        pass

    return merged


def create_instrument_config_for_equity(
    symbol: str,
    token: Optional[int] = None,
    current_price: float = 1000.0,
    atr_14: Optional[float] = None,
) -> InstrumentConfig:
    """
    Factory creating an InstrumentConfig for an equity constituent.
    Calibrates min_orb_range, max_orb_range, and max_risk_cap proportionate
    to the equity's price, exactly preserving the index calibration ratio
    (40 / 120 / 80 points on a 24,000 index = ~0.17% / 0.50% / 0.33%).
    """
    price = max(current_price, 10.0)

    # 40 / 24,000 = 0.00167
    min_orb = round(max(price * 0.0017, 0.5), 2)
    # 120 / 24,000 = 0.00500
    max_orb = round(max(price * 0.0060, min_orb * 2.5), 2)
    # 80 / 24,000 = 0.00333
    max_risk = round(max(price * 0.0040, min_orb * 1.5), 2)

    return InstrumentConfig(
        symbol=symbol,
        exchange="NSE",
        instrument_type=InstrumentType.EQUITY,
        lot_size=1,  # Equity MIS trades in single share increments
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
    Custom 300-stock scanning universe (100 Large Cap, 100 Mid Cap, 100 Small Cap).
    Classification is sourced from a maintained local dataset at data/universe/300_stocks.json.
    """

    def __init__(self, json_path: Optional[Path] = None):
        self.json_path = json_path or (
            Path(__file__).resolve().parent.parent / "data" / "universe" / "300_stocks.json"
        )
        self._records: List[StockRecord] = []
        self._load()

    def _load(self) -> None:
        if not self.json_path.exists():
            raise FileNotFoundError(f"Universe data dataset file not found: {self.json_path}")
        with open(self.json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._records = [
            StockRecord(
                symbol=item["symbol"],
                name=item["name"],
                market_cap_rank=int(item["market_cap_rank"]),
                category=str(item["category"]).lower(),
            )
            for item in data
        ]

    @property
    def large_cap_100(self) -> List[StockRecord]:
        return [r for r in self._records if r.category == "large"]

    @property
    def mid_cap_100(self) -> List[StockRecord]:
        return [r for r in self._records if r.category == "mid"]

    @property
    def small_cap_100(self) -> List[StockRecord]:
        return [r for r in self._records if r.category == "small"]

    @property
    def all_stocks(self) -> List[StockRecord]:
        return list(self._records)

    def print_startup_summary(self, token_map: Dict[str, int]) -> Dict[str, Any]:
        """
        Prints startup validation counts for Large/Mid/Small cap categories
        and instrument token resolution state.
        """
        large_cnt = len(self.large_cap_100)
        mid_cnt = len(self.mid_cap_100)
        small_cnt = len(self.small_cap_100)
        total_cnt = len(self.all_stocks)

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
    Resolves Kite instrument tokens for all 300 stocks in the scanning universe.
    1. Reads local JSON cache if available.
    2. Downloads NSE instrument dump via kite.instruments("NSE") once if missing/forced.
    3. Caches result to data/cache/universe_300_tokens.json.
    """
    target_cache = cache_path or DEFAULT_300_CACHE_FILE
    universe = StockUniverse()
    target_symbols = [r.symbol for r in universe.all_stocks]

    # 1. Read from cache if valid
    if target_cache.exists() and not force_refresh:
        try:
            with open(target_cache, "r", encoding="utf-8") as f:
                data = json.load(f)
            if any(sym in data for sym in target_symbols):
                logger.info(f"Loaded {len(data)} instrument tokens from cache: {target_cache}")
                return data
        except Exception as e:
            logger.warning(f"Error reading token cache from {target_cache}: {e}")

    # 2. Resolve via live Kite dump
    resolved: Dict[str, int] = {}
    if kite_client is not None:
        try:
            client = getattr(kite_client, "kite", kite_client)
            if hasattr(client, "instruments"):
                logger.info("Resolving 300-stock universe instrument tokens via kite.instruments('NSE')...")
                all_nse = client.instruments("NSE")
                target_set = set(target_symbols)
                for inst in all_nse:
                    sym = inst.get("tradingsymbol")
                    if sym in target_set:
                        resolved[sym] = int(inst["instrument_token"])

                target_cache.parent.mkdir(parents=True, exist_ok=True)
                with open(target_cache, "w", encoding="utf-8") as f:
                    json.dump(resolved, f, indent=2)
                logger.info(f"Resolved and cached {len(resolved)}/300 tokens to {target_cache}")
                return resolved
        except Exception as e:
            logger.warning(f"Failed to fetch live instrument dump from Kite: {e}")

    # 3. Merge with fallback offline tokens
    merged = dict(_FALLBACK_NSE_TOKENS)
    merged.update(resolved)
    return merged

