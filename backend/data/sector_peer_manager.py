from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import pandas as pd

from backend.config.settings import settings
from backend.data.time_utils import now_ist_naive
from backend.monitoring.logger import logger

if TYPE_CHECKING:
    from backend.data.models import PeerContext


PEER_STATUS_AVAILABLE = "AVAILABLE"
PEER_STATUS_UNAVAILABLE = "UNAVAILABLE"

STALE_PEER_DATA_REASON = "stale sector peer data"


class PeerDataUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class PeerContextResult:
    status: str
    context: Optional[Any]
    reason: str
    required_timestamp: Optional[datetime] = None
    frame_latest: Dict[str, Optional[datetime]] = field(default_factory=dict)
    stale: bool = False


@dataclass
class SectorDefinition:
    name: str
    primary_leader: str
    secondary_leader: str
    market_index: str = "NIFTY"
    sector_index: str = ""
    constituents: Tuple[str, ...] = ()


SECTOR_METADATA_PATH = (
    Path(__file__).resolve().parent
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

        sector_index = str(
            raw.get("sector_index") or ""
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

        if not sector_index:
            raise ValueError(
                f"Sector {sector_name!r} is missing sector_index."
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
                sector_index=sector_index,
                constituents=constituents,
            )
        )

    return definitions


SECTOR_DEFINITIONS: Dict[str, SectorDefinition] = _load_sector_definitions()

DEFAULT_SECTOR: Optional[SectorDefinition] = SECTOR_DEFINITIONS.get("INFRA_CAPGOODS_REALTY") or (next(iter(SECTOR_DEFINITIONS.values()), None) if SECTOR_DEFINITIONS else None)


class SectorPeerManager:

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
                or sym == sec.sector_index
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
            if DEFAULT_SECTOR is not None:
                return DEFAULT_SECTOR
            logger.warning(
                "No authoritative sector classification for symbol %s",
                sym,
            )
            return None

        return matches[0]

    @classmethod
    def get_peer_symbols(cls, symbol: str) -> Tuple[str, str, str]:
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

    @staticmethod
    def _validate_peer_frame(
        df: Optional[pd.DataFrame],
        sym: str,
        required: datetime,
    ) -> Tuple[Optional[pd.DataFrame], str, str, Optional[datetime]]:
        from backend.data.candle_aggregator import _normalize_ist_naive

        if (
            df is None
            or df.empty
            or "datetime" not in df.columns
            or "close" not in df.columns
        ):
            return None, "invalid", f"{sym}: peer data empty or missing datetime/close columns", None

        frame = df.copy()
        frame["datetime"] = pd.to_datetime(
            frame["datetime"].map(_normalize_ist_naive),
            errors="coerce",
        )
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frame = frame[
            frame["datetime"].notna()
            & frame["close"].notna()
            & (frame["close"] > 0)
        ]
        frame = frame[frame["datetime"] <= pd.Timestamp(required)]
        frame = (
            frame.sort_values("datetime", kind="stable")
            .drop_duplicates(subset=["datetime"], keep="last")
            .reset_index(drop=True)
        )

        if frame.empty:
            return None, "invalid", f"{sym}: no valid completed candles at or before {required}", None

        latest = frame["datetime"].iloc[-1].to_pydatetime()
        if latest != required:
            return (
                None,
                "stale",
                f"{STALE_PEER_DATA_REASON}: {sym} latest candle {latest} != required {required}",
                latest,
            )

        return frame, "ok", "", latest

    @classmethod
    def _load_peer_frame(
        cls,
        sym: str,
        c_dir: Path,
        required: datetime,
        kite_client: Optional[Any],
        allow_network_fetch: bool,
    ) -> Tuple[Optional[pd.DataFrame], str, str, Optional[datetime]]:
        from backend.data.historical_loader import HistoricalDataLoader

        failures: List[Tuple[str, str, Optional[datetime]]] = []

        c_file = c_dir / f"{sym}_15m.csv"
        candidates: List[Tuple[Path, bool]] = [(c_file, True)]
        if sym == "NIFTY":
            candidates.extend(
                (c_dir / alt_name, False)
                for alt_name in ("NIFTY_15m.csv", "NIFTY50_15m.csv")
            )

        seen: set = set()
        for path, check_identity in candidates:
            if path in seen or not path.exists():
                continue
            seen.add(path)
            try:
                df, meta = HistoricalDataLoader.load_cached_data_with_validation(path)
                if (
                    check_identity
                    and meta is not None
                    and str(meta.get("symbol", "")).upper() != sym.upper()
                ):
                    raise ValueError(f"Cache symbol identity mismatch for {sym}")
            except Exception as exc:
                logger.debug("Failed to load cached 15m data for %s: %s", sym, exc)
                failures.append(("invalid", f"{sym}: cache load failed ({exc})", None))
                continue

            frame, kind, message, latest = cls._validate_peer_frame(df, sym, required)
            if frame is not None:
                return frame, "ok", "", latest
            failures.append((kind, message, latest))

        if allow_network_fetch and kite_client is not None:
            try:
                from backend.data.instrument_resolver import instrument_resolver

                tok = instrument_resolver.resolve_token(sym, exchange="NSE", kite_client=kite_client)
                if tok:
                    today = now_ist_naive().date()
                    start_d = today - timedelta(days=45)
                    fetched = HistoricalDataLoader.fetch_real_data(
                        kite_client=kite_client,
                        instrument_token=tok,
                        start_date=start_d,
                        end_date=today,
                        interval="15minute",
                        cache_path=c_file,
                    )
                    frame, kind, message, latest = cls._validate_peer_frame(fetched, sym, required)
                    if frame is not None:
                        return frame, "ok", "", latest
                    failures.append((kind, message, latest))
                else:
                    failures.append(("missing", f"{sym}: instrument token unavailable", None))
            except Exception as exc:
                logger.debug("Failed to fetch live Kite data for peer %s: %s", sym, exc)
                failures.append(("invalid", f"{sym}: live fetch failed ({exc})", None))

        for preferred in ("stale", "invalid"):
            for kind, message, latest in failures:
                if kind == preferred:
                    return None, kind, message, latest

        if failures:
            kind, message, latest = failures[0]
            return None, kind, message, latest

        return None, "missing", f"{sym}: peer data not available in cache", None

    @classmethod
    def build_peer_context_with_status(
        cls,
        symbol: str,
        cache_dir: Optional[Path] = None,
        kite_client: Optional[Any] = None,
        allow_network_fetch: bool = False,
    ) -> PeerContextResult:
        from backend.data.historical_loader import HistoricalDataLoader
        from backend.data.models import PeerContext
        from backend.data.candle_aggregator import _normalize_ist_naive

        def unavailable(
            reason: str,
            required: Optional[datetime] = None,
            latest: Optional[Dict[str, Optional[datetime]]] = None,
            stale: bool = False,
        ) -> PeerContextResult:
            return PeerContextResult(
                status=PEER_STATUS_UNAVAILABLE,
                context=None,
                reason=reason,
                required_timestamp=required,
                frame_latest=latest or {},
                stale=stale,
            )

        c_dir = Path(cache_dir) if cache_dir is not None else (settings.base_dir / "backend" / "data" / "cache")
        try:
            leader_sym, market_sym, sector_sym = cls.get_peer_symbols(symbol)
        except ValueError as exc:
            logger.debug("Cannot build PeerContext for %s: %s", symbol, exc)
            return unavailable(f"no sector classification for {symbol}")

        try:
            required = _normalize_ist_naive(
                HistoricalDataLoader.get_latest_completed_candle_start(now_ist_naive())
            )
        except Exception as exc:
            return unavailable(f"required completed-candle timestamp could not be determined ({exc})")

        if required is None:
            return unavailable("no completed candle exists yet for the required session")

        frames: Dict[str, Optional[pd.DataFrame]] = {}
        latest_map: Dict[str, Optional[datetime]] = {}
        problems: List[str] = []
        any_stale = False

        for role, sym in (("leader", leader_sym), ("market", market_sym), ("sector", sector_sym)):
            frame, kind, message, latest = cls._load_peer_frame(
                sym, c_dir, required, kite_client, allow_network_fetch
            )
            frames[role] = frame
            latest_map[sym] = latest
            if frame is None:
                problems.append(f"{role}({sym}): {message}")
                if kind == "stale":
                    any_stale = True

        if problems:
            prefix = STALE_PEER_DATA_REASON if any_stale else "sector peer data unavailable"
            reason = f"{prefix} for {symbol}: " + "; ".join(problems)
            logger.warning("SIT PeerContext %s", reason)
            return unavailable(reason, required, latest_map, any_stale)

        df_leader = frames["leader"]
        df_market = frames["market"]
        df_sector = frames["sector"]

        if "volume" not in df_leader.columns:
            reason = f"sector peer data unavailable for {symbol}: leader {leader_sym} has no volume column (Returning None to prevent fabricated data)"
            logger.warning("SIT PeerContext %s", reason)
            return unavailable(reason, required, latest_map)

        try:
            context = PeerContext(leader=df_leader, market=df_market, sector=df_sector)
        except Exception as exc:
            logger.warning("Failed to initialize PeerContext for %s: %s", symbol, exc)
            return unavailable(
                f"sector peer data unavailable for {symbol}: PeerContext initialization failed ({exc})",
                required,
                latest_map,
            )

        return PeerContextResult(
            status=PEER_STATUS_AVAILABLE,
            context=context,
            reason="",
            required_timestamp=required,
            frame_latest=latest_map,
        )

    @classmethod
    def build_peer_context(
        cls,
        symbol: str,
        cache_dir: Optional[Path] = None,
        kite_client: Optional[Any] = None,
        allow_network_fetch: bool = False,
        raise_on_unavailable: bool = False,
    ) -> Optional[PeerContext]:
        result = cls.build_peer_context_with_status(
            symbol,
            cache_dir=cache_dir,
            kite_client=kite_client,
            allow_network_fetch=allow_network_fetch,
        )
        if result.status == PEER_STATUS_AVAILABLE:
            return result.context
        if raise_on_unavailable:
            raise PeerDataUnavailableError(f"{result.status}: {result.reason}")
        return None

    @classmethod
    def get_sector_name(cls, symbol: str) -> Optional[str]:
        sec = cls.get_sector_for_symbol(symbol)
        return sec.name if sec is not None else None


def get_sector_index_symbol(
    symbol: str,
) -> Optional[str]:
    sec = SectorPeerManager.get_sector_for_symbol(symbol)

    if sec is None:
        return None

    return sec.sector_index or sec.market_index


sector_peer_manager = SectorPeerManager()