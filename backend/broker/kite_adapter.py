"""
Zerodha Kite Connect session/client adapter.

Responsibilities
----------------
- Own the local Kite session file.
- Save, load, validate, and clear the authenticated Kite session.
- Provide one process-wide validated KiteConnect client.
- Provide a small compatibility adapter for authentication/client access.
- Provide read-only Level-5 BookSnapshot parsing from a real Kite quote.

This module does NOT:
- place orders
- modify orders
- cancel orders
- read positions for trading decisions
- read margins
- own a KiteTicker WebSocket
- run the market-stream pipeline

The production WebSocket owner is:
    streaming.market_stream_manager
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException

from backend.config.settings import settings
from backend.data.time_utils import now_ist_naive
from backend.monitoring.logger import logger


IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Process-wide validated Kite client
# ---------------------------------------------------------------------------

_cached_kite: Optional[KiteConnect] = None
_kite_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------

def _normalize_ist_naive(value: Any) -> Optional[datetime]:
    """
    Convert a datetime-like value to naive Asia/Kolkata time.

    Missing/invalid values return None.

    IMPORTANT:
    There is intentionally NO datetime.now() fallback here.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()

        if not raw:
            return None

        try:
            dt = datetime.fromisoformat(
                raw.replace("Z", "+00:00")
            )
        except ValueError:
            return None
    else:
        return None

    try:
        if dt.tzinfo is not None:
            return (
                dt.astimezone(IST)
                .replace(tzinfo=None)
            )

        return dt
    except (TypeError, ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# Session file handling
# ---------------------------------------------------------------------------

def _session_payload(
    api_key: str,
    access_token: str,
    user_id: str = "",
    user_name: str = "Trader",
    public_token: str = "",
) -> Dict[str, Any]:
    """
    Build the persisted session payload.

    login_time is generated only when a new authenticated session is saved.
    """
    return {
        "api_key": str(api_key).strip(),
        "user_id": str(user_id or ""),
        "user_name": str(user_name or "Trader"),
        "access_token": str(access_token).strip(),
        "public_token": str(public_token or ""),
        "login_time": now_ist_naive().isoformat(),
    }


def _write_session_securely(
    path: Path,
    payload: Dict[str, Any],
) -> None:
    """
    Atomically replace the session file with restrictive permissions.

    The file is created with mode 0600 before the authenticated credentials
    are written. A temporary file in the same directory is used so os.replace()
    remains atomic on supported filesystems.
    """
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd: Optional[int] = None
    temp_path: Optional[Path] = None

    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=".session_token.",
            suffix=".tmp",
            dir=str(path.parent),
            text=True,
        )

        temp_path = Path(temp_name)

        # Restrict permissions before writing credentials.
        try:
            os.chmod(
                temp_path,
                0o600,
            )
        except OSError:
            pass

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as file_obj:
            fd = None

            json.dump(
                payload,
                file_obj,
                indent=2,
            )

            file_obj.flush()
            os.fsync(
                file_obj.fileno()
            )

        try:
            os.chmod(
                temp_path,
                0o600,
            )
        except OSError:
            pass

        os.replace(
            temp_path,
            path,
        )

        # Keep the final file restrictive as an additional safeguard.
        try:
            os.chmod(
                path,
                0o600,
            )
        except OSError:
            pass

    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

        if temp_path is not None:
            try:
                temp_path.unlink(
                    missing_ok=True
                )
            except OSError:
                pass

        raise


def save_session(
    api_key: str,
    access_token: str,
    user_id: str = "",
    user_name: str = "Trader",
    public_token: str = "",
) -> Dict[str, Any]:
    """
    Persist a newly generated Kite session and update the process cache.
    """
    global _cached_kite

    clean_api_key = str(
        api_key or ""
    ).strip()

    clean_access_token = str(
        access_token or ""
    ).strip()

    if not clean_api_key:
        raise ValueError(
            "Cannot save Kite session: api_key is empty."
        )

    if not clean_access_token:
        raise ValueError(
            "Cannot save Kite session: access_token is empty."
        )

    payload = _session_payload(
        api_key=clean_api_key,
        access_token=clean_access_token,
        user_id=user_id,
        user_name=user_name,
        public_token=public_token,
    )

    _write_session_securely(
        settings.token_file,
        payload,
    )

    kite = KiteConnect(
        api_key=clean_api_key
    )

    kite.set_access_token(
        clean_access_token
    )

    with _kite_lock:
        _cached_kite = kite

    return payload


def get_saved_session() -> Optional[Dict[str, Any]]:
    """
    Load the authenticated Kite session.

    Priority:
        1. secure session_token.json
        2. .env provisioned API key/access token

    Invalid or incomplete sessions return None.
    """
    token_file = settings.token_file

    if token_file.exists():
        try:
            with open(
                token_file,
                "r",
                encoding="utf-8",
            ) as file_obj:
                data = json.load(
                    file_obj
                )

            if not isinstance(
                data,
                dict,
            ):
                logger.warning(
                    "Kite session file does not contain an object."
                )
            else:
                api_key = data.get(
                    "api_key"
                )
                access_token = data.get(
                    "access_token"
                )

                if (
                    isinstance(api_key, str)
                    and api_key.strip()
                    and isinstance(access_token, str)
                    and access_token.strip()
                ):
                    return data

                logger.warning(
                    "Kite session file is missing required credentials."
                )

        except (
            OSError,
            json.JSONDecodeError,
        ) as exc:
            logger.warning(
                "Failed to read Kite session file %s: %s",
                token_file,
                exc,
            )

        except Exception:
            logger.exception(
                "Unexpected error while reading Kite session file %s",
                token_file,
            )

    # Environment fallback.
    env_api_key = (
        settings.kite_api_key
    )

    env_access_token = (
        settings.kite_access_token
    )

    if (
        env_api_key
        and env_access_token
        and env_api_key != "your_api_key_here"
    ):
        return {
            "api_key": str(
                env_api_key
            ).strip(),
            "access_token": str(
                env_access_token
            ).strip(),
            "user_id": str(
                settings.kite_user_id or ""
            ),
            "user_name": "Trader",
            "login_time": None,
        }

    return None


def clear_session() -> None:
    """
    Clear the process cache and delete the local session file.
    """
    global _cached_kite

    with _kite_lock:
        _cached_kite = None
        kite_broker_adapter.disconnect()

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return

    token_file = settings.token_file

    if not token_file.exists():
        return

    try:
        token_file.unlink()
    except OSError as exc:
        logger.warning(
            "Error deleting Kite session file %s: %s",
            token_file,
            exc,
        )


# ---------------------------------------------------------------------------
# Active Kite client
# ---------------------------------------------------------------------------

def get_active_kite_with_diagnostics(
    force_validate: bool = False,
) -> Tuple[
    Optional[KiteConnect],
    Optional[str],
]:
    """
    Return (validated KiteConnect client, error).

    The session is validated with Kite profile() when the in-memory client
    is absent or force_validate=True.

    Network errors do NOT delete the stored session.
    A confirmed TokenException does.
    """
    global _cached_kite

    session = get_saved_session()

    if not session:
        with _kite_lock:
            _cached_kite = None

        return (
            None,
            "No saved Kite session found. "
            "Please log in with Kite Connect.",
        )

    api_key = session.get(
        "api_key"
    )

    access_token = session.get(
        "access_token"
    )

    if (
        not isinstance(api_key, str)
        or not api_key.strip()
        or not isinstance(access_token, str)
        or not access_token.strip()
    ):
        with _kite_lock:
            _cached_kite = None

        return (
            None,
            "Kite session exists but required credentials are missing.",
        )

    api_key = api_key.strip()
    access_token = access_token.strip()

    # Fast path.
    if (
        _cached_kite is not None
        and not force_validate
    ):
        return (
            _cached_kite,
            None,
        )

    should_clear = False
    last_error_type = ""
    last_error_message = ""

    with _kite_lock:

        # Double-check after lock acquisition.
        if (
            _cached_kite is not None
            and not force_validate
        ):
            return (
                _cached_kite,
                None,
            )

        for attempt in range(2):
            try:
                kite = KiteConnect(
                    api_key=api_key
                )

                kite.set_access_token(
                    access_token
                )

                profile = kite.profile()

                if (
                    isinstance(profile, dict)
                    and profile.get("user_id")
                ):
                    _cached_kite = kite

                    return (
                        kite,
                        None,
                    )

                _cached_kite = None

                return (
                    None,
                    "Kite profile validation returned no user_id.",
                )

            except TokenException as exc:
                last_error_type = type(
                    exc
                ).__name__

                last_error_message = str(
                    exc
                )

                should_clear = True

                logger.warning(
                    "Kite authentication token is invalid or expired "
                    "(attempt %d).",
                    attempt + 1,
                )

                # A TokenException is definitive. No need to retry it.
                break

            except Exception as exc:
                last_error_type = type(
                    exc
                ).__name__

                last_error_message = str(
                    exc
                )

                if attempt == 0:
                    logger.warning(
                        "Kite session validation attempt 1 failed "
                        "(%s). Retrying once.",
                        last_error_type,
                    )

                    # Do not hold up the process indefinitely.
                    import time

                    time.sleep(
                        0.5
                    )

                else:
                    logger.warning(
                        "Kite session validation failed on retry (%s).",
                        last_error_type,
                    )

        _cached_kite = None

    if should_clear:
        clear_session()

        return (
            None,
            "Kite access token is invalid or expired. "
            "Please authenticate again.",
        )

    return (
        None,
        (
            f"Kite session validation failed "
            f"({last_error_type})."
        ),
    )


def get_active_kite() -> Optional[KiteConnect]:
    """
    Return the active validated KiteConnect client, or None.
    """
    kite, _ = (
        get_active_kite_with_diagnostics(
            force_validate=False
        )
    )

    return kite


# ---------------------------------------------------------------------------
# Minimal compatibility adapter
# ---------------------------------------------------------------------------

class KiteBrokerAdapter:
    """
    Small compatibility wrapper around the shared Kite client.

    This class intentionally does NOT own WebSocket streaming or account/
    portfolio operations. The production stream is owned by
    MarketStreamManager.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> None:
        self.name = "KITE_CLIENT"

        self.api_key = (
            api_key.strip()
            if isinstance(api_key, str)
            and api_key.strip()
            else None
        )

        self.access_token = (
            access_token.strip()
            if isinstance(access_token, str)
            and access_token.strip()
            else None
        )

        self.kite: Optional[
            KiteConnect
        ] = None

        self.is_connected = False

    def _load_saved_token(self) -> bool:
        """
        Load credentials from the shared session source.
        """
        session = get_saved_session()

        if not session:
            return False

        if not self.access_token:
            token = session.get(
                "access_token"
            )

            if token:
                self.access_token = str(
                    token
                ).strip()

        if not self.api_key:
            key = session.get(
                "api_key"
            )

            if key:
                self.api_key = str(
                    key
                ).strip()

        return bool(
            self.api_key
            and self.access_token
        )

    def connect(self) -> bool:
        """
        Validate this adapter's Kite credentials.

        No WebSocket is started.
        """
        if not (
            self.api_key
            and self.access_token
        ):
            self._load_saved_token()

        if not (
            self.api_key
            and self.access_token
        ):
            logger.error(
                "[%s] Cannot connect: API key or access token is missing.",
                self.name,
            )

            self.is_connected = False

            return False

        try:
            kite = KiteConnect(
                api_key=self.api_key
            )

            kite.set_access_token(
                self.access_token
            )

            profile = kite.profile()

            if not (
                isinstance(profile, dict)
                and profile.get("user_id")
            ):
                raise RuntimeError(
                    "Kite profile validation returned no user_id."
                )

            self.kite = kite
            self.is_connected = True

            logger.info(
                "[%s] Connected as user %s.",
                self.name,
                profile.get("user_id"),
            )

            return True

        except Exception as exc:
            self.kite = None
            self.is_connected = False

            logger.error(
                "[%s] Kite authentication validation failed: %s",
                self.name,
                type(exc).__name__,
            )

            return False

    def disconnect(self) -> None:
        """
        Disconnect this local client reference.

        KiteTicker ownership is intentionally not handled here.
        """
        self.kite = None
        self.is_connected = False

        logger.info(
            "[%s] Disconnected.",
            self.name,
        )


# ---------------------------------------------------------------------------
# Read-only Level-5 quote parser
# ---------------------------------------------------------------------------

def make_book_snapshot_from_quote(
    quote_dict: dict,
    symbol: str,
    timestamp: Optional[datetime] = None,
) -> Optional[Any]:
    """
    Convert a real Kite quote() response into BookSnapshot.

    No timestamp is fabricated.

    A snapshot is returned only when:
    - depth exists
    - at least five buy levels exist
    - at least five sell levels exist
    - a valid timestamp exists
    - prices/quantities are valid
    """
    if not isinstance(
        quote_dict,
        dict,
    ):
        return None

    depth = quote_dict.get(
        "depth"
    )

    if not isinstance(
        depth,
        dict,
    ):
        return None

    buy_levels = depth.get(
        "buy",
        []
    )

    sell_levels = depth.get(
        "sell",
        []
    )

    if (
        not isinstance(buy_levels, list)
        or not isinstance(sell_levels, list)
        or len(buy_levels) < 5
        or len(sell_levels) < 5
    ):
        return None

    raw_timestamp = quote_dict.get(
        "timestamp"
    )

    quote_timestamp = (
        _normalize_ist_naive(
            raw_timestamp
        )
    )

    supplied_timestamp = (
        _normalize_ist_naive(
            timestamp
        )
    )

    snapshot_timestamp = (
        quote_timestamp
        or supplied_timestamp
    )

    # NEVER use datetime.now() here.
    if snapshot_timestamp is None:
        logger.warning(
            "Rejecting quote for %s because no valid timestamp exists.",
            symbol,
        )

        return None

    def parse_levels(
        levels: list,
    ) -> Optional[list]:
        parsed = []

        for level in levels[:5]:
            if not isinstance(
                level,
                dict,
            ):
                return None

            try:
                price = float(level["price"])
                quantity = int(level["quantity"])

                raw_orders = level.get("orders")
                if raw_orders is None:
                    return None

                orders = int(raw_orders)

            except (
                TypeError,
                ValueError,
                KeyError,
            ):
                return None

            if (
                price <= 0
                or quantity < 0
                or orders < 0
            ):
                return None

            parsed.append(
                (
                    price,
                    quantity,
                    orders,
                )
            )

        return parsed

    bids = parse_levels(
        buy_levels
    )

    asks = parse_levels(
        sell_levels
    )

    if bids is None or asks is None:
        return None

    try:
        ltp = float(
            quote_dict.get(
                "last_price",
                0.0,
            )
        )
    except (
        TypeError,
        ValueError,
    ):
        return None

    if ltp <= 0:
        return None

    fut_ltp = (
        ltp
        if "FUT" in str(symbol).upper()
        else None
    )

    raw_oi = quote_dict.get(
        "oi"
    )

    try:
        fut_oi = (
            float(raw_oi)
            if raw_oi is not None
            else None
        )
    except (
        TypeError,
        ValueError,
    ):
        fut_oi = None

    def positive_float(
        key: str,
    ) -> Optional[float]:
        value = quote_dict.get(
            key
        )

        if value is None:
            return None

        try:
            number = float(
                value
            )
        except (
            TypeError,
            ValueError,
        ):
            return None

        return (
            number
            if number > 0
            else None
        )

    from backend.strategy.ssf_l5_srm_strategy import (
        BookSnapshot,
    )

    return BookSnapshot(
        timestamp=snapshot_timestamp,
        bids=bids,
        asks=asks,
        ltp=ltp,
        fut_ltp=fut_ltp,
        fut_oi=fut_oi,
        circuit_lower=positive_float(
            "lower_circuit_limit"
        ),
        circuit_upper=positive_float(
            "upper_circuit_limit"
        ),
    )


KiteBrokerAdapter.make_book_snapshot_from_quote = staticmethod(make_book_snapshot_from_quote)


# ---------------------------------------------------------------------------
# Compatibility singleton
# ---------------------------------------------------------------------------

kite_broker_adapter = KiteBrokerAdapter()