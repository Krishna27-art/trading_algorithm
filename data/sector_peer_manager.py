"""
Sector and Peer Relationship Manager.
Provides sector classification, sector leaders, and PeerContext construction
for SectorImpulseStrategy across the 700-stock universe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import pandas as pd

from config.settings import settings
from data.time_utils import now_ist_naive
from monitoring.logger import logger

if TYPE_CHECKING:
    from strategy.sector_impulse_strategy import PeerContext


@dataclass
class SectorDefinition:
    name: str
    primary_leader: str
    secondary_leader: str
    market_index: str = "NIFTY"
    constituents: Tuple[str, ...] = ()


SECTOR_METADATA_PATH = (
    settings.base_dir
    / "data"
    / "universe"
    / "sector_classification.json"
)


def _load_sector_definitions() -> Dict[str, SectorDefinition]:
    if not SECTOR_METADATA_PATH.exists():
        raise FileNotFoundError(
            f"Sector metadata file not found: "
            f"{SECTOR_METADATA_PATH}"
        )

    with open(
        SECTOR_METADATA_PATH,
        "r",
        encoding="utf-8",
    ) as handle:
        payload = json.load(handle)

    if not isinstance(payload, dict):
        raise ValueError(
            "Sector metadata must be a JSON object."
        )

    source = payload.get("source")
    effective_date = payload.get("effective_date")
    raw_sectors = payload.get("sectors")

    if not source:
        raise ValueError(
            "Sector metadata source is missing."
        )

    if not effective_date:
        raise ValueError(
            "Sector metadata effective_date is missing."
        )

    if not isinstance(raw_sectors, dict) or not raw_sectors:
        raise ValueError(
            "Sector metadata contains no sectors."
        )

    definitions: Dict[str, SectorDefinition] = {}

    for sector_name, raw in raw_sectors.items():
        if not isinstance(raw, dict):
            raise ValueError(
                f"Invalid metadata for sector {sector_name!r}."
            )

        primary = str(
            raw.get("primary_leader") or ""
        ).strip().upper()

        secondary = str(
            raw.get("secondary_leader") or ""
        ).strip().upper()

        market_index = str(
            raw.get("market_index") or ""
        ).strip().upper()

        constituents = tuple(
            str(symbol).strip().upper()
            for symbol in raw.get(
                "constituents",
                [],
            )
            if str(symbol).strip()
        )

        if not primary or not secondary:
            raise ValueError(
                f"Sector {sector_name!r} is missing leaders."
            )

        if not market_index:
            raise ValueError(
                f"Sector {sector_name!r} is missing market_index."
            )

        if not constituents:
            raise ValueError(
                f"Sector {sector_name!r} has no constituents."
            )

        definitions[str(sector_name).upper()] = (
            SectorDefinition(
                name=str(sector_name).upper(),
                primary_leader=primary,
                secondary_leader=secondary,
                market_index=market_index,
                constituents=constituents,
            )
        )

    return definitions


# Authoritative sector groupings for Indian Equities (NSE 700 Universe)
SECTOR_DEFINITIONS: Dict[str, SectorDefinition] = _load_sector_definitions()

# General fallback for symbols not explicitly classified
DEFAULT_SECTOR: Optional[SectorDefinition] = None


class SectorPeerManager:
    """Manages sector classification and peer context loading for SIT."""

    @classmethod
    def get_sector_for_symbol(
        cls,
        symbol: str,
    ) -> Optional[SectorDefinition]:
        sym = symbol.strip().upper()

        matches = []

        for sec in SECTOR_DEFINITIONS.values():
            if (
                sym in sec.constituents
                or sym == sec.primary_leader
                or sym == sec.secondary_leader
            ):
                matches.append(sec)

        if len(matches) > 1:
            logger.error(
                "Ambiguous sector classification for %s: %s",
                sym,
                [sec.name for sec in matches],
            )
            return None

        if not matches:
            logger.warning(
                "No authoritative sector classification for symbol %s",
                sym,
            )
            return None

        return matches[0]

    @classmethod
    def get_peer_symbols(cls, symbol: str) -> Tuple[str, str, str]:
        """
        Returns (leader_symbol, market_symbol, sector_symbol) for a given symbol.
        If symbol IS the primary leader, the secondary leader acts as the peer leader.
        """
        sec = cls.get_sector_for_symbol(symbol)
        if sec is None:
            raise ValueError(
                f"No authoritative sector classification for {symbol}"
            )
        sym = symbol.strip().upper()

        if sym == sec.primary_leader:
            leader = sec.secondary_leader
            sector = sec.primary_leader
        else:
            leader = sec.primary_leader
            sector = sec.secondary_leader

        market = sec.market_index
        return leader, market, sector

    @classmethod
    def build_peer_context(
        cls,
        symbol: str,
        cache_dir: Optional[Path] = None,
        kite_client: Optional[Any] = None,
    ) -> Optional[PeerContext]:
        """
        Constructs a real PeerContext for SectorImpulseStrategy using cached
        or live 15m historical candles for leader, market, and sector representative.
        """
        from data.historical_loader import HistoricalDataLoader
        from strategy.sector_impulse_strategy import PeerContext

        c_dir = Path(cache_dir) if cache_dir is not None else (settings.base_dir / "data" / "cache")
        try:
            leader_sym, market_sym, sector_sym = cls.get_peer_symbols(symbol)
        except ValueError as e:
            logger.debug("Cannot build PeerContext for %s: %s", symbol, e)
            return None

        def load_df(sym: str) -> Optional[pd.DataFrame]:
            latest_completed = (
                HistoricalDataLoader.get_latest_completed_candle_start(
                    now_ist_naive()
                )
            )

            cached_candidate = None
            c_file = c_dir / f"{sym}_15m.csv"
            if c_file.exists():
                try:
                    df, _ = HistoricalDataLoader.load_cached_data_with_validation(c_file)
                    if not df.empty and "datetime" in df.columns and "close" in df.columns:
                        df["datetime"] = pd.to_datetime(df["datetime"])
                        if latest_completed is None or df["datetime"].max() >= latest_completed:
                            return df
                        if df["datetime"].max().date() >= (now_ist_naive().date() - timedelta(days=4)):
                            cached_candidate = df
                except Exception as e:
                    logger.debug("Failed to load cached 15m data for %s: %s", sym, e)

            # Fall back to NIFTY cache if market is NIFTY
            if sym == "NIFTY":
                for alt_name in (
                    "NIFTY_15m.csv",
                    "NIFTY50_15m.csv",
                    "NIFTY_15m_180d.csv",
                ):
                    alt_file = c_dir / alt_name
                    if alt_file.exists():
                        try:
                            df, _ = HistoricalDataLoader.load_cached_data_with_validation(alt_file)
                            if not df.empty and "datetime" in df.columns and "close" in df.columns:
                                df["datetime"] = pd.to_datetime(df["datetime"])
                                if latest_completed is None or df["datetime"].max() >= latest_completed:
                                    return df
                                if df["datetime"].max().date() >= (now_ist_naive().date() - timedelta(days=4)):
                                    cached_candidate = df
                        except Exception as exc:
                            logger.debug("Failed to load NIFTY cache %s: %s", alt_file, exc)

            # If Kite client is available, fetch real historical data
            if kite_client is not None:
                try:
                    from data.instrument_resolver import instrument_resolver
                    tok = instrument_resolver.resolve_token(sym, exchange="NSE", kite_client=kite_client)
                    if tok:
                        today = now_ist_naive().date()
                        start_d = today - timedelta(days=45)
                        df = HistoricalDataLoader.fetch_real_data(
                            kite_client=kite_client,
                            instrument_token=tok,
                            start_date=start_d,
                            end_date=today,
                            interval="15minute",
                            cache_path=c_file,
                        )
                        if not df.empty:
                            return df
                except Exception as e:
                    logger.debug("Failed to fetch live Kite data for peer %s: %s", sym, e)

            if cached_candidate is not None:
                logger.warning(
                    "Cached peer context for %s is stale relative to the latest completed candle; refusing stale live context.",
                    sym,
                )

            return None

        df_leader = load_df(leader_sym)
        df_market = load_df(market_sym)
        df_sector = load_df(sector_sym)

        # Ensure leader has real volume column; refuse to fabricate volume data.
        if df_leader is not None and "volume" not in df_leader.columns:
            logger.warning(
                "SIT requires real leader volume; "
                "volume column is absent for %s. "
                "Returning None to prevent fabricated data from "
                "reaching the strategy.",
                leader_sym,
            )
            return None

        # If any essential frame is missing, return None
        if df_leader is None or df_market is None or df_sector is None:
            logger.debug(
                "SIT PeerContext incomplete for %s: "
                "leader(%s)=%s, "
                "market(%s)=%s, "
                "sector(%s)=%s",
                symbol,
                leader_sym,
                "OK" if df_leader is not None else "MISSING",
                market_sym,
                "OK" if df_market is not None else "MISSING",
                sector_sym,
                "OK" if df_sector is not None else "MISSING",
            )
            return None

        try:
            return PeerContext(leader=df_leader, market=df_market, sector=df_sector)
        except Exception as e:
            logger.warning("Failed to initialize PeerContext for %s: %s", symbol, e)
            return None

    @classmethod
    def get_sector_name(cls, symbol: str) -> Optional[str]:
        """Return the sector name string for a given symbol."""
        sec = cls.get_sector_for_symbol(symbol)
        return sec.name if sec is not None else None


def get_sector_index_symbol(
    symbol: str,
) -> Optional[str]:
    """Return the NSE sector index symbol corresponding to the stock symbol."""
    sec = SectorPeerManager.get_sector_for_symbol(symbol)

    if sec is None:
        return None

    return sec.market_index


sector_peer_manager = SectorPeerManager()
