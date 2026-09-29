"""
Sector and Peer Relationship Manager.
Provides sector classification, sector leaders, and PeerContext construction
for SectorImpulseStrategy across the 300-stock universe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from config.settings import settings
from monitoring.logger import logger
from strategy.sector_impulse_strategy import PeerContext


@dataclass
class SectorDefinition:
    name: str
    primary_leader: str
    secondary_leader: str
    market_index: str = "NIFTY"
    constituents: Tuple[str, ...] = ()


# Authoritative sector groupings for Indian Equities (NSE 300 Universe)
SECTOR_DEFINITIONS: Dict[str, SectorDefinition] = {
    "IT": SectorDefinition(
        name="IT",
        primary_leader="TCS",
        secondary_leader="INFY",
        market_index="NIFTY",
        constituents=(
            "TCS", "INFY", "HCLTECH", "WIPRO", "TECHM", "LTIM", "PERSISTENT", "COFORGE",
            "MPHASIS", "LTTS", "TATAELXSI", "ZENSARTECH", "CYIENT", "KPITTECH", "SONACOMS",
            "CAMS", "ECLERX", "BSOFT", "OFSS", "MASTEK", "HAPPSTMNDS", "TANLA",
        ),
    ),
    "BANKING": SectorDefinition(
        name="BANKING",
        primary_leader="HDFCBANK",
        secondary_leader="ICICIBANK",
        market_index="NIFTY",
        constituents=(
            "HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "INDUSINDBK",
            "BANKBARODA", "PNB", "FEDERALBNK", "IDFCFIRSTB", "AUBANK", "BANDHANBNK",
            "CANBK", "UNIONBANK", "INDIANB", "UCOBANK", "UJJIVANSFB", "MAHABANK",
            "CENTRALBK", "IOB", "PSB", "KARURVYSYA", "CUB", "RBLBANK",
        ),
    ),
    "AUTO": SectorDefinition(
        name="AUTO",
        primary_leader="MARUTI",
        secondary_leader="TMPV",
        market_index="NIFTY",
        constituents=(
            "MARUTI", "TMPV", "TATAMOTORS", "M&M", "BAJAJ-AUTO", "EICHERMOT", "HEROMOTOCO",
            "TVSMOTOR", "ASHOKLEY", "BHARATFORG", "MOTHERSON", "BOSCHLTD", "BALKRISIND",
            "MRF", "APOLLOTYRE", "SONACOMS", "EXIDEIND", "ARE&M", "AMARAJA", "ENDURANCE",
            "TIINDIA", "CRAFTSMAN", "CEATLTD",
        ),
    ),
    "ENERGY_POWER": SectorDefinition(
        name="ENERGY_POWER",
        primary_leader="RELIANCE",
        secondary_leader="NTPC",
        market_index="NIFTY",
        constituents=(
            "RELIANCE", "NTPC", "POWERGRID", "ONGC", "BPCL", "IOC", "COALINDIA", "GAIL",
            "TATAPOWER", "ADANIGREEN", "ADANIPOWER", "NHPC", "SJVN", "OIL", "PETRONET",
            "IGL", "MGL", "GUJGASLTD", "TORNTPOWER", "CESC", "SUZLON",
        ),
    ),
    "METALS": SectorDefinition(
        name="METALS",
        primary_leader="TATASTEEL",
        secondary_leader="JSWSTEEL",
        market_index="NIFTY",
        constituents=(
            "TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "JINDALSTEL", "NMDC", "SAIL",
            "NATIONALUM", "HINDZINC", "APLAPOLLO", "WELCORP", "RATNAMANI", "JSL",
        ),
    ),
    "PHARMA_HEALTH": SectorDefinition(
        name="PHARMA_HEALTH",
        primary_leader="SUNPHARMA",
        secondary_leader="DRREDDY",
        market_index="NIFTY",
        constituents=(
            "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "APOLLOHOSP", "MANKIND",
            "ZYDUSLIFE", "TORNTPHARM", "LUPIN", "AUROPHARMA", "BIOCON", "GLENMARK",
            "IPCALAB", "LAURUSLABS", "SYNGENE", "FORTIS", "MAXHEALTH", "NATCOPHARM",
            "JBCHEMPHARM", "GRANULES", "MARKSANS", "NEULANDLAB", "AJANTPHARM", "ALKEM",
        ),
    ),
    "FMCG_CONSUMER": SectorDefinition(
        name="FMCG_CONSUMER",
        primary_leader="HINDUNILVR",
        secondary_leader="ITC",
        market_index="NIFTY",
        constituents=(
            "HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "TATACONSUM", "VBL",
            "GODREJCP", "DABUR", "MARICO", "COLPAL", "PGHH", "EMAMILTD", "RADICO",
            "UBL", "MCDOWELL-N", "DEVYANI", "JUBLFOOD", "BIKAJI", "GODREJAGRO",
        ),
    ),
    "FIN_SERVICES": SectorDefinition(
        name="FIN_SERVICES",
        primary_leader="BAJFINANCE",
        secondary_leader="BAJAJFINSV",
        market_index="NIFTY",
        constituents=(
            "BAJFINANCE", "BAJAJFINSV", "SHRIRAMFIN", "CHOLAFIN", "MUTHOOTFIN", "SBILIFE",
            "HDFCLIFE", "ICICIPRULI", "ICICIGI", "HDFCAMC", "PFC", "RECLTD", "IREDA",
            "HUDCO", "LICHSGFIN", "CANFINHOME", "PNBHOUSING", "POONAWALLA", "MANAPPURAM",
            "CREDITACC", "MASFIN", "SUNDARMFIN", "SHAREINDIA", "ANGELONE", "CDSL",
        ),
    ),
    "INFRA_CAPGOODS_REALTY": SectorDefinition(
        name="INFRA_CAPGOODS_REALTY",
        primary_leader="LT",
        secondary_leader="SIEMENS",
        market_index="NIFTY",
        constituents=(
            "LT", "SIEMENS", "ABB", "BHEL", "BEL", "HAL", "HAVELLS", "POLYCAB", "KEI",
            "CUMMINSIND", "VOLTAS", "BLUESTARCO", "THERMAX", "ASTRAL", "SUPREMEIND",
            "DLF", "GODREJPROP", "OBERORLTY", "PHOENIXLTD", "BRIGADE", "PRESTIGE",
            "SOBHA", "NBCC", "GMRINFRA", "IRB", "NCC", "PNCINFRA", "KNRCON", "RITES",
            "IRCON", "RVNL", "ENGINERSIN", "KPIL", "MTARTECH", "MTARTECH-BE", "ADOR",
        ),
    ),
    "CHEMICALS": SectorDefinition(
        name="CHEMICALS",
        primary_leader="PIDILITIND",
        secondary_leader="SRF",
        market_index="NIFTY",
        constituents=(
            "PIDILITIND", "SRF", "AARTIIND", "DEEPAKNTR", "TATACHEM", "ATUL",
            "NAVINFLUOR", "VINATIORGA", "FINEORG", "ALKYLAMINE", "BALAMINES", "CLEAN",
            "SUMICHEM", "FLUOROCHEM", "STYRENIX", "ANDHRSUGAR", "HEG", "HEGAM",
        ),
    ),
}

# General fallback for symbols not explicitly classified
DEFAULT_SECTOR = SectorDefinition(
    name="GENERAL_EQUITY",
    primary_leader="RELIANCE",
    secondary_leader="HDFCBANK",
    market_index="NIFTY",
)


class SectorPeerManager:
    """Manages sector classification and peer context loading for SIT."""

    @classmethod
    def get_sector_for_symbol(cls, symbol: str) -> SectorDefinition:
        sym = symbol.strip().upper()
        for sec in SECTOR_DEFINITIONS.values():
            if sym in sec.constituents or sym == sec.primary_leader or sym == sec.secondary_leader:
                return sec
        return DEFAULT_SECTOR

    @classmethod
    def get_peer_symbols(cls, symbol: str) -> Tuple[str, str, str]:
        """
        Returns (leader_symbol, market_symbol, sector_symbol) for a given symbol.
        If symbol IS the primary leader, the secondary leader acts as the peer leader.
        """
        sec = cls.get_sector_for_symbol(symbol)
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

        c_dir = cache_dir or (settings.base_dir / "data" / "cache")
        leader_sym, market_sym, sector_sym = cls.get_peer_symbols(symbol)

        def load_df(sym: str) -> Optional[pd.DataFrame]:
            # Try specific cached file first
            c_file = c_dir / f"{sym}_15m.csv"
            if c_file.exists():
                try:
                    df, _ = HistoricalDataLoader.load_cached_data_with_validation(c_file)
                    if not df.empty and "datetime" in df.columns and "close" in df.columns:
                        return df
                except Exception as e:
                    logger.debug(f"Failed to load cached 15m data for {sym}: {e}")

            # Fall back to NIFTY cache if market is NIFTY
            if sym == "NIFTY":
                for alt_name in ("NIFTY_15m.csv", "NIFTY50_15m.csv", "NIFTY_15m_180d.csv"):
                    alt_file = c_dir / alt_name
                    if alt_file.exists():
                        try:
                            df, _ = HistoricalDataLoader.load_cached_data_with_validation(alt_file)
                            if not df.empty and "datetime" in df.columns and "close" in df.columns:
                                return df
                        except Exception:
                            pass
                # Fall back to RELIANCE or HDFCBANK as market proxy
                for proxy in ("RELIANCE", "HDFCBANK", "TCS"):
                    proxy_file = c_dir / f"{proxy}_15m.csv"
                    if proxy_file.exists():
                        try:
                            df, _ = HistoricalDataLoader.load_cached_data_with_validation(proxy_file)
                            if not df.empty:
                                return df
                        except Exception:
                            pass

            # If Kite client is available, fetch real historical data
            if kite_client is not None:
                try:
                    from data.instrument_resolver import instrument_resolver
                    tok = instrument_resolver.resolve_token(sym, exchange="NSE", kite_client=kite_client)
                    if tok:
                        today = datetime.now().date()
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
                    logger.debug(f"Failed to fetch live Kite data for peer {sym}: {e}")

            return None

        df_leader = load_df(leader_sym)
        df_market = load_df(market_sym)
        df_sector = load_df(sector_sym)

        # Ensure leader has volume column
        if df_leader is not None and "volume" not in df_leader.columns:
            df_leader["volume"] = 1000

        # If any essential frame is missing, return None
        if df_leader is None or df_market is None or df_sector is None:
            logger.debug(
                f"SIT PeerContext incomplete for {symbol}: "
                f"leader({leader_sym})={'OK' if df_leader is not None else 'MISSING'}, "
                f"market({market_sym})={'OK' if df_market is not None else 'MISSING'}, "
                f"sector({sector_sym})={'OK' if df_sector is not None else 'MISSING'}"
            )
            return None

        try:
            return PeerContext(leader=df_leader, market=df_market, sector=df_sector)
        except Exception as e:
            logger.warning(f"Failed to initialize PeerContext for {symbol}: {e}")
            return None
