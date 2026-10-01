"""
Central WebSocket stream manager coordinating Zerodha KiteTicker,
MultiSymbolCandleAggregator, LiveMarketState, and LiveSignalEngine.
"""

from __future__ import annotations

from enum import Enum
import logging
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from broker.kite_adapter import get_saved_session
from config.universe import resolve_universe_tokens
from data.candle_aggregator import MultiSymbolCandleAggregator
from data.time_utils import now_ist, now_ist_iso
from streaming.live_market_state import live_market_state
from streaming.live_signal_engine import live_signal_engine

logger = logging.getLogger("streaming.market_stream_manager")


class StreamState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


class MarketStreamManager:
    """Singleton manager for the KiteTicker WebSocket streaming pipeline."""

    def __init__(self):
        self._lock = threading.Lock()
        self.state: StreamState = StreamState.DISCONNECTED
        self.token_to_symbol: Dict[int, str] = {}
        self.subscribed_token_count: int = 0
        self.tick_count: int = 0
        self.candle_count: int = 0
        self.last_tick_time: Optional[datetime] = None
        self.last_connect_time: Optional[datetime] = None
        self.last_disconnect_time: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.kws: Optional[Any] = None
        self.aggregator: Optional[MultiSymbolCandleAggregator] = None

    def start_stream(
        self,
        token_to_symbol: Optional[Dict[int, str]] = None,
        kite_client: Optional[Any] = None,
        timeframe_minutes: int = 15,
    ) -> Dict[str, Any]:
        """
        Starts the KiteTicker WebSocket stream.
        If token_to_symbol is not provided, automatically resolves universe tokens.
        """
        with self._lock:
            # Stop any existing stream before starting a new one
            if self.kws is not None and self.state == StreamState.CONNECTED:
                self._stop_internal()

            # Resolve tokens if not supplied
            if not token_to_symbol:
                token_map = resolve_universe_tokens(kite_client=kite_client)
                token_to_symbol = {int(tok): sym for sym, tok in token_map.items() if tok}

            if not token_to_symbol:
                raise ValueError("No valid instrument tokens available to start market stream.")

            session = get_saved_session()
            if not session:
                self.state = StreamState.ERROR
                self.last_error = "No active Zerodha Kite session found. Please authenticate via Kite login."
                raise RuntimeError(self.last_error)

            api_key = session.get("api_key")
            access_token = session.get("access_token")
            if not api_key or not access_token:
                self.state = StreamState.ERROR
                self.last_error = "Invalid session credentials."
                raise RuntimeError(self.last_error)

            self.token_to_symbol = dict(token_to_symbol)
            self.subscribed_token_count = len(token_to_symbol)
            self.state = StreamState.CONNECTING
            self.last_error = None

            # Initialize LiveMarketState with the token map
            live_market_state.set_token_map(self.token_to_symbol)

            # Define callbacks for candle close and Level-5 book updates
            def _on_candle_close(candle_dict: dict, vwap: float):
                with self._lock:
                    self.candle_count += 1
                live_market_state.update_candle_close(candle_dict, vwap)
                live_signal_engine.on_candle_close(candle_dict, vwap, kite_client=kite_client)

            def _on_book_update(symbol: str, snapshot: Any):
                live_market_state.update_book_snapshot(symbol, snapshot)

            self.aggregator = MultiSymbolCandleAggregator(
                token_to_symbol_map=self.token_to_symbol,
                timeframe_minutes=timeframe_minutes,
                on_candle_close=_on_candle_close,
                on_book_update=_on_book_update,
            )

            try:
                from kiteconnect import KiteTicker
            except ImportError:
                self.state = StreamState.ERROR
                self.last_error = "kiteconnect package is not installed."
                raise RuntimeError(self.last_error)

            kws = KiteTicker(api_key, access_token)
            tokens = list(self.token_to_symbol.keys())

            def on_ticks(ws, ticks):
                now_ts = now_ist()
                with self._lock:
                    self.tick_count += len(ticks) if isinstance(ticks, list) else 1
                    self.last_tick_time = now_ts

                for t in (ticks if isinstance(ticks, list) else [ticks]):
                    tok = t.get("instrument_token")
                    sym = self.token_to_symbol.get(tok)
                    last_price = t.get("last_price")
                    vol = t.get("volume", 0)
                    if sym and last_price is not None:
                        live_market_state.update_tick(
                            symbol=sym,
                            price=float(last_price),
                            volume=int(vol),
                            timestamp=now_ts.replace(tzinfo=None),
                            token=tok,
                        )

                if self.aggregator:
                    try:
                        self.aggregator.process_ticks(ticks)
                    except Exception as e:
                        logger.error(f"[MarketStreamManager] Error processing ticks: {e}")

            def on_connect(ws, response):
                with self._lock:
                    self.state = StreamState.CONNECTED
                    self.last_connect_time = now_ist()
                ws.subscribe(tokens)
                ws.set_mode(ws.MODE_FULL, tokens)
                logger.info(
                    f"[MarketStreamManager] KiteTicker connected in MODE_FULL — "
                    f"subscribed {len(tokens)} instruments."
                )

            def on_close(ws, code, reason):
                with self._lock:
                    self.state = StreamState.DISCONNECTED
                    self.last_disconnect_time = now_ist()
                logger.warning(f"[MarketStreamManager] KiteTicker closed: code={code} reason={reason}")

            def on_error(ws, code, reason):
                with self._lock:
                    self.state = StreamState.ERROR
                    self.last_error = f"code={code} reason={reason}"
                logger.error(f"[MarketStreamManager] KiteTicker error: {self.last_error}")

            def on_reconnect(ws, attempts_count):
                with self._lock:
                    self.state = StreamState.RECONNECTING
                logger.info(f"[MarketStreamManager] KiteTicker reconnecting (attempt {attempts_count})...")

            kws.on_ticks = on_ticks
            kws.on_connect = on_connect
            kws.on_close = on_close
            kws.on_error = on_error
            kws.on_reconnect = on_reconnect

            self.kws = kws
            kws.connect(threaded=True)
            logger.info("[MarketStreamManager] KiteTicker stream connecting in background.")

            return {
                "status": "CONNECTING",
                "subscribed_tokens": len(self.token_to_symbol),
                "symbols_count": len(self.token_to_symbol),
            }

    def _stop_internal(self) -> None:
        if self.kws is not None:
            try:
                self.kws.close()
            except Exception as e:
                logger.warning(f"[MarketStreamManager] Error closing KiteTicker: {e}")
            self.kws = None
        self.state = StreamState.STOPPED
        self.last_disconnect_time = now_ist()

    def stop_stream(self) -> Dict[str, Any]:
        with self._lock:
            self._stop_internal()
            return {"status": "STOPPED", "state": self.state.value}

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            is_connected = self.state == StreamState.CONNECTED and self.kws is not None
            return {
                "state": self.state.value,
                "connected": is_connected,
                "subscribed_token_count": self.subscribed_token_count,
                "tick_count": self.tick_count,
                "candle_count": self.candle_count,
                "last_tick_time": self.last_tick_time.isoformat() if self.last_tick_time else None,
                "last_connect_time": self.last_connect_time.isoformat() if self.last_connect_time else None,
                "last_disconnect_time": self.last_disconnect_time.isoformat() if self.last_disconnect_time else None,
                "last_error": self.last_error,
            }


market_stream_manager = MarketStreamManager()
