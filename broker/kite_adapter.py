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

    def place_order(
        self,
        symbol: str,
        direction: OrderDirection,
        order_type: OrderType,
        quantity: int,
        price: Optional[float] = None,
        tag: Optional[str] = "algo",
        client_order_id: Optional[str] = None,
        product: Optional[str] = "MIS",
        exchange: Optional[str] = None,
    ) -> OrderRecord:
        if not self.kite:
            raise RuntimeError("Kite adapter is not connected.")

        txn_type = self.kite.TRANSACTION_TYPE_BUY if direction == OrderDirection.BUY else self.kite.TRANSACTION_TYPE_SELL
        ord_type = self.kite.ORDER_TYPE_LIMIT if order_type == OrderType.LIMIT else self.kite.ORDER_TYPE_MARKET
        target_exchange = exchange or (self.kite.EXCHANGE_NFO if getattr(self, "default_exchange", "NSE") == "NFO" else self.kite.EXCHANGE_NSE)

        prod_upper = (product or "MIS").upper()
        if prod_upper == "CNC":
            target_product = self.kite.PRODUCT_CNC
        elif prod_upper == "NRML":
            target_product = self.kite.PRODUCT_NRML
        else:
            target_product = self.kite.PRODUCT_MIS

        # Kite tag field has an 8-character or 20-character limit depending on API version; use tag or slice client_order_id
        order_tag = tag or (client_order_id[:8] if client_order_id else "algo")

        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=target_exchange,
                tradingsymbol=symbol,
                transaction_type=txn_type,
                quantity=quantity,
                product=target_product,
                order_type=ord_type,
                price=price,
                tag=order_tag,
            )
            logger.info(f"[{self.name}] Order placed successfully. Broker Order ID: {order_id}")
            return OrderRecord(
                order_id=order_id,
                broker_order_id=order_id,
                client_order_id=client_order_id,
                symbol=symbol,
                direction=direction,
                order_type=order_type,
                price=price,
                quantity=quantity,
                status=OrderStatus.SUBMITTED,
                created_at=datetime.now(),
                updated_at=datetime.now(),
                tag=order_tag,
                product=prod_upper,
                exchange=target_exchange,
            )
        except Exception as e:
            logger.error(f"[{self.name}] Order placement failed: {e}")
            raise

    def cancel_order(self, order_id: str) -> bool:
        if not self.kite:
            return False
        try:
            self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=order_id)
            return True
        except Exception as e:
            logger.error(f"[{self.name}] Cancel order failed for {order_id}: {e}")
            return False

    def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        if not self.kite:
            return 0
        try:
            orders = self.kite.orders()
            cancelled = 0
            for o in orders:
                if o.get("status") in ("OPEN", "TRIGGER PENDING"):
                    if symbol and o.get("tradingsymbol") != symbol:
                        continue
                    self.cancel_order(o["order_id"])
                    cancelled += 1
            return cancelled
        except Exception as e:
            logger.error(f"[{self.name}] Error in cancel_all_orders: {e}")
            return 0

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
        instruments: List[Any],
        on_tick: Optional[Callable[[Any], None]] = None,
        mode: str = "full",
    ) -> Optional[KiteTicker]:
        """
        Starts live market data streaming via Zerodha KiteTicker.
        - Uses authenticated API key & access token.
        - Subscribes to specified instrument tokens.
        - Sets subscription mode ('full' or 'quote').
        - Handles reconnects and error logging automatically.
        """
        if not self.api_key or not self.access_token:
            logger.error(f"[{self.name}] Cannot start KiteTicker stream: Missing api_key or access_token.")
            return None

        from data.instrument_resolver import instrument_resolver

        tokens: List[int] = []
        for item in instruments:
            if isinstance(item, int):
                tokens.append(item)
            elif isinstance(item, str):
                tok = instrument_resolver.resolve_token(item, exchange="NSE", kite_client=self)
                if tok:
                    tokens.append(tok)
                else:
                    logger.warning(f"[{self.name}] Unresolved token for symbol '{item}'")
            elif hasattr(item, "instrument_token") and item.instrument_token:
                tokens.append(int(item.instrument_token))
            elif hasattr(item, "symbol") and item.symbol:
                tok = instrument_resolver.resolve_token(item.symbol, exchange="NSE", kite_client=self)
                if tok:
                    tokens.append(tok)

        tokens = sorted(list(set(tokens)))
        if not tokens:
            logger.error(f"[{self.name}] Cannot start market stream: No valid instrument tokens to subscribe.")
            return None

        if on_tick:
            self.tick_callback = on_tick

        def _on_connect(ws, response):
            logger.info(f"[{self.name}] KiteTicker WebSocket connected. Subscribing to {len(tokens)} tokens...")
            try:
                ws.subscribe(tokens)
                ticker_mode = ws.MODE_FULL if mode.lower() == "full" else ws.MODE_QUOTE
                ws.set_mode(ticker_mode, tokens)
                logger.info(f"[{self.name}] Subscribed to {len(tokens)} tokens in '{mode}' mode.")
            except Exception as e:
                logger.error(f"[{self.name}] Error setting mode/subscription on connect: {e}")

        def _on_ticks(ws, ticks):
            if self.tick_callback:
                try:
                    self.tick_callback(ticks)
                except Exception as e:
                    logger.error(f"[{self.name}] Error in tick callback execution: {e}")

        def _on_close(ws, code, reason):
            logger.warning(f"[{self.name}] KiteTicker connection closed. Code: {code}, Reason: {reason}")

        def _on_error(ws, code, reason):
            logger.error(f"[{self.name}] KiteTicker error. Code: {code}, Reason: {reason}")

        def _on_reconnect(ws, attempt_count):
            logger.info(f"[{self.name}] KiteTicker reconnecting... (Attempt {attempt_count})")

        def _on_noreconnect(ws):
            logger.error(f"[{self.name}] KiteTicker max reconnect attempts reached. Stream stopped.")

        try:
            if self.kws and hasattr(self.kws, "is_connected") and self.kws.is_connected():
                self.kws.subscribe(tokens)
                ticker_mode = self.kws.MODE_FULL if mode.lower() == "full" else self.kws.MODE_QUOTE
                self.kws.set_mode(ticker_mode, tokens)
                logger.info(f"[{self.name}] Updated existing KiteTicker stream with {len(tokens)} tokens.")
                return self.kws

            self.kws = KiteTicker(api_key=self.api_key, access_token=self.access_token)
            self.kws.on_connect = _on_connect
            self.kws.on_ticks = _on_ticks
            self.kws.on_close = _on_close
            self.kws.on_error = _on_error
            self.kws.on_reconnect = _on_reconnect
            self.kws.on_noreconnect = _on_noreconnect

            self.kws.connect(threaded=True)
            logger.info(f"[{self.name}] KiteTicker WebSocket stream launched in background thread.")
            return self.kws
        except Exception as e:
            logger.error(f"[{self.name}] Failed to launch KiteTicker stream: {e}")
            return None

    def stop_market_stream(self) -> None:
        """Stops and closes active KiteTicker WebSocket connection."""
        if self.kws:
            try:
                logger.info(f"[{self.name}] Closing KiteTicker WebSocket stream...")
                self.kws.close()
            except Exception as e:
                logger.warning(f"[{self.name}] Error closing KiteTicker WebSocket: {e}")
            finally:
                self.kws = None