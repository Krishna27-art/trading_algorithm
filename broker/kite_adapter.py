"""
Zerodha Kite Connect v3 Broker Adapter.
Handles REST order routing and KiteTicker WebSocket data streaming with automatic reconnects.

This module is also the single place that owns the on-disk Kite session
(``session_token.json``): reading it, validating it against the live API,
caching the validated client in memory, and clearing it on logout. Nothing
else in the codebase should read/write that file or hold its own KiteConnect
cache — call the module-level functions below instead.
"""

import json
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from kiteconnect import KiteConnect, KiteTicker

from broker.base_broker import BaseBrokerAdapter
from config.settings import settings
from database.models import OrderDirection, OrderRecord, OrderStatus, OrderType
from monitoring.logger import logger


# ---------------------------------------------------------------------------
# Shared Kite session (single source of truth for session_token.json)
# ---------------------------------------------------------------------------

# Process-wide cache of the last-validated KiteConnect client, so repeated
# requests within a session don't each re-hit the /profile endpoint.
_cached_kite: Optional[KiteConnect] = None


def save_session(
    api_key: str,
    access_token: str,
    user_id: str = "",
    user_name: str = "Trader",
    public_token: str = "",
) -> Dict[str, Any]:
    """Persists a freshly generated Kite session to settings.token_file and
    updates the in-memory cache. Returns the saved payload."""
    global _cached_kite
    payload = {
        "api_key": api_key,
        "user_id": user_id,
        "user_name": user_name,
        "access_token": access_token,
        "public_token": public_token,
        "login_time": datetime.now().isoformat(),
    }
    settings.token_file.parent.mkdir(parents=True, exist_ok=True)
    with open(settings.token_file, "w") as f:
        json.dump(payload, f, indent=2)
    try:
        settings.token_file.chmod(0o600)
    except OSError:
        pass  # best-effort on platforms without POSIX permissions

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    _cached_kite = kite
    return payload


def get_saved_session() -> Optional[Dict[str, Any]]:
    """Reads the saved Kite session with an .env fallback for environments
    that provision a long-lived access token instead of a token file."""
    if settings.token_file.exists():
        try:
            with open(settings.token_file, "r") as f:
                data = json.load(f)
                if data.get("access_token") and data.get("api_key"):
                    return data
        except Exception as e:
            logger.warning(f"Failed to read session file {settings.token_file}: {e}")

    if settings.kite_api_key and settings.kite_access_token and settings.kite_api_key != "your_api_key_here":
        return {
            "api_key": settings.kite_api_key,
            "access_token": settings.kite_access_token,
            "user_id": settings.kite_user_id or "",
            "user_name": "Trader",
            "login_time": datetime.now().isoformat(),
        }

    return None


def clear_session() -> None:
    """Clears the in-memory cache and deletes the on-disk session file."""
    global _cached_kite
    _cached_kite = None
    if settings.token_file.exists():
        try:
            settings.token_file.unlink()
        except Exception as e:
            logger.warning(f"Error deleting token file {settings.token_file}: {e}")


def get_active_kite_with_diagnostics(force_validate: bool = False) -> Tuple[Optional[KiteConnect], Optional[str]]:
    """Returns (KiteConnect instance, error message). Exactly one is None."""
    global _cached_kite

    session = get_saved_session()
    if not session:
        _cached_kite = None
        return None, "No saved Kite session found. Please log in with Kite Connect."

    api_key = session.get("api_key")
    access_token = session.get("access_token")
    if not api_key or not access_token:
        _cached_kite = None
        return None, "Session file exists but is missing api_key or access_token."

    if _cached_kite is not None and not force_validate:
        return _cached_kite, None

    try:
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)
        profile = kite.profile()
        if profile and "user_id" in profile:
            _cached_kite = kite
            return kite, None
        _cached_kite = None
        return None, "Kite profile check returned empty profile."
    except Exception as e:
        err_type = type(e).__name__
        err_msg = str(e)
        logger.warning(f"Kite session validation failed ({err_type}): {err_msg}")
        _cached_kite = None
        if "TokenException" in err_type or "403" in err_msg or "expired" in err_msg.lower():
            clear_session()
        return None, f"Kite session error ({err_type}): {err_msg}"


def get_active_kite() -> Optional[KiteConnect]:
    """Convenience getter returning the active Kite client, or None."""
    kite, _ = get_active_kite_with_diagnostics(force_validate=False)
    return kite


class KiteBrokerAdapter(BaseBrokerAdapter):
    def __init__(self, api_key: Optional[str] = None, access_token: Optional[str] = None):
        super().__init__(name="KITE_BROKER")
        self.api_key = api_key
        self.access_token = access_token
        self.kite: Optional[KiteConnect] = None
        self.kws: Optional[KiteTicker] = None
        self.tick_callback: Optional[Callable[[float, int, datetime], None]] = None

    def _load_saved_token(self) -> bool:
        """Falls back to the shared on-disk session (see get_saved_session
        above) when this adapter wasn't constructed with explicit credentials."""
        session = get_saved_session()
        if session and session.get("access_token"):
            self.access_token = session["access_token"]
            if not self.api_key:
                self.api_key = session.get("api_key")
            return True
        return False

    def connect(self) -> bool:
        if not self.access_token:
            self._load_saved_token()

        if not self.api_key or not self.access_token:
            logger.error(f"[{self.name}] Cannot connect: API Key or Access Token is missing. Run auth first.")
            return False

        try:
            self.kite = KiteConnect(api_key=self.api_key)
            self.kite.set_access_token(self.access_token)
            profile = self.kite.profile()
            self.is_connected = True
            logger.info(f"[{self.name}] Connected as: {profile.get('user_name')} ({profile.get('user_id')})")
            return True
        except Exception as e:
            logger.error(f"[{self.name}] Authentication validation error: {e}")
            self.is_connected = False
            return False

    def disconnect(self):
        if self.kws:
            try:
                self.kws.close()
            except Exception:
                pass
        self.is_connected = False
        logger.info(f"[{self.name}] Disconnected.")



    def get_positions(self) -> List[Dict[str, Any]]:
        if not self.kite:
            return []
        try:
            pos = self.kite.positions()
            return pos.get("net", [])
        except Exception as e:
            logger.error(f"[{self.name}] Error fetching positions: {e}")
            return []

    def get_orders(self) -> List[Dict[str, Any]]:
        if not self.kite:
            return []
        try:
            return self.kite.orders()
        except Exception as e:
            logger.error(f"[{self.name}] Error fetching orders: {e}")
            return []

    def get_margins(self) -> Dict[str, float]:
        if not self.kite:
            return {"net": 0.0, "available_cash": 0.0}
        try:
            margins = self.kite.margins()
            equity = margins.get("equity", {})
            return {
                "net": float(equity.get("net", 0.0)),
                "available_cash": float(equity.get("available", {}).get("live_balance", 0.0)),
            }
        except Exception as e:
            logger.error(f"[{self.name}] Error fetching margins: {e}")
            return {"net": 0.0, "available_cash": 0.0}

    def start_market_stream(self, symbols: List[str], on_tick: Callable[[float, int, datetime], None]):
        self.tick_callback = on_tick
        logger.info(f"[{self.name}] KiteTicker stream configured for {symbols}.")