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
import threading
import time as _time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from kiteconnect import KiteConnect, KiteTicker

from broker.base_broker import BaseBrokerAdapter
from config.settings import settings
from data.candle_aggregator import MultiSymbolCandleAggregator
from database.models import OrderDirection, OrderRecord, OrderStatus, OrderType
from monitoring.logger import logger


# ---------------------------------------------------------------------------
# Shared Kite session (single source of truth for session_token.json)
# ---------------------------------------------------------------------------

# Process-wide cache of the last-validated KiteConnect client, so repeated
# requests within a session don't each re-hit the /profile endpoint.
_cached_kite: Optional[KiteConnect] = None
_kite_lock = threading.Lock()


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
    with _kite_lock:
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
    with _kite_lock:
        _cached_kite = None
    if settings.token_file.exists():
        try:
            settings.token_file.unlink()
        except Exception as e:
            logger.warning(f"Error deleting token file {settings.token_file}: {e}")


def get_active_kite_with_diagnostics(force_validate: bool = False) -> Tuple[Optional[KiteConnect], Optional[str]]:
    """Returns (KiteConnect instance, error message). Exactly one is None.

    Thread-safe: uses _kite_lock so that on a cold start only one thread
    actually calls ``kite.profile()`` while the others wait and reuse the
    result.  This prevents Kite API rate-limit failures from poisoning the
    cache for concurrent requests.
    """
    global _cached_kite

    session = get_saved_session()
    if not session:
        with _kite_lock:
            _cached_kite = None
        return None, "No saved Kite session found. Please log in with Kite Connect."

    api_key = session.get("api_key")
    access_token = session.get("access_token")
    if not api_key or not access_token:
        with _kite_lock:
            _cached_kite = None
        return None, "Session file exists but is missing api_key or access_token."

    # Fast path: return already-validated client without acquiring the lock
    # (reads of a Python object reference are atomic on CPython).
    if _cached_kite is not None and not force_validate:
        return _cached_kite, None

    # Slow path: only one thread validates at a time.
    _should_clear = False
    _err_msg_out = ""
    with _kite_lock:
        # Double-check after acquiring the lock — another thread may have
        # already validated while we were waiting.
        if _cached_kite is not None and not force_validate:
            return _cached_kite, None

        # Try up to 2 times — the first failure may be a transient race
        # (e.g. server reload, network blip). Only clear the session file
        # if both attempts confirm the token is genuinely invalid.
        last_err_type = ""
        last_err_msg = ""
        for attempt in range(2):
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
                last_err_type = type(e).__name__
                last_err_msg = str(e)
                if attempt == 0:
                    logger.info(f"Kite session validation attempt 1 failed ({last_err_type}): {last_err_msg} — retrying...")
                    _time.sleep(0.5)
                else:
                    logger.warning(f"Kite session validation failed on retry ({last_err_type}): {last_err_msg}")

        # Both attempts failed
        _cached_kite = None
        _err_msg_out = f"Kite session error ({last_err_type}): {last_err_msg}"
        # Only delete the session file for confirmed TokenException (invalid/
        # expired token), never for transient network/timeout errors.
        if "TokenException" in last_err_type:
            _should_clear = True

    # Clear session file outside the lock to avoid deadlock
    if _should_clear:
        clear_session()
    return None, _err_msg_out


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

    def start_market_stream(
        self,
        token_to_symbol: Dict[int, str],
        on_candle_close: Callable[[dict, float], None],
        timeframe_minutes: int = 15,
    ) -> None:
        """
        Fix 2.3: Start a real KiteTicker WebSocket connection.
        Ticks are routed through MultiSymbolCandleAggregator which fires
        on_candle_close(candle_dict, vwap) each time a 15-minute candle closes.

        token_to_symbol: {instrument_token: "SYMBOL"} for all instruments to subscribe.
        on_candle_close: callback invoked when a candle completes.
        """
        if not token_to_symbol:
            logger.warning(f"[{self.name}] start_market_stream called with no tokens.")
            return

        session = get_saved_session()
        if not session:
            raise RuntimeError("[KiteBrokerAdapter] No active Kite session. Run auth.py first.")

        api_key = session["api_key"]
        access_token = session["access_token"]

        # Build the candle aggregator
        self._aggregator = MultiSymbolCandleAggregator(
            token_to_symbol_map=token_to_symbol,
            timeframe_minutes=timeframe_minutes,
            on_candle_close=on_candle_close,
        )
        self.tick_callback = on_candle_close
        tokens = list(token_to_symbol.keys())

        # Build and connect KiteTicker
        kws = KiteTicker(api_key, access_token)

        def on_ticks(ws, ticks):
            try:
                self._aggregator.process_ticks(ticks)
            except Exception as e:
                logger.error(f"[{self.name}] Error processing ticks: {e}")

        def on_connect(ws, response):
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)
            logger.info(
                f"[{self.name}] KiteTicker connected — subscribed {len(tokens)} instruments "
                f"({', '.join(str(t) for t in tokens[:5])}{'...' if len(tokens) > 5 else ''})"
            )

        def on_close(ws, code, reason):
            logger.warning(f"[{self.name}] KiteTicker closed: code={code} reason={reason}")

        def on_error(ws, code, reason):
            logger.error(f"[{self.name}] KiteTicker error: code={code} reason={reason}")

        def on_reconnect(ws, attempts_count):
            logger.info(f"[{self.name}] KiteTicker reconnecting (attempt {attempts_count})...")

        kws.on_ticks = on_ticks
        kws.on_connect = on_connect
        kws.on_close = on_close
        kws.on_error = on_error
        kws.on_reconnect = on_reconnect

        self.kws = kws
        kws.connect(threaded=True)
        logger.info(f"[{self.name}] KiteTicker stream started (threaded).")

    def stop_market_stream(self) -> None:
        """Gracefully close the KiteTicker WebSocket connection."""
        if self.kws:
            try:
                self.kws.close()
                logger.info(f"[{self.name}] KiteTicker stream stopped.")
            except Exception as e:
                logger.warning(f"[{self.name}] Error stopping KiteTicker: {e}")
            finally:
                self.kws = None
        if hasattr(self, "_aggregator"):
            self._aggregator = None


# ---------------------------------------------------------------------------
# Module-level singleton for use by backend/stream.py and other modules that
# need to start/stop the WebSocket market stream. This follows the same
# pattern as the shared _cached_kite for the REST session.
# ---------------------------------------------------------------------------
kite_broker_adapter = KiteBrokerAdapter()