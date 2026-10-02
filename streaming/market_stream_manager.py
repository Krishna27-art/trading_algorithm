"""
Central WebSocket stream manager coordinating Zerodha KiteTicker,
MultiSymbolCandleAggregator, LiveMarketState, and LiveSignalEngine.
"""

from __future__ import annotations

from enum import Enum
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from broker.kite_adapter import get_saved_session, get_active_kite
from config.settings import settings
from config.universe import resolve_universe_tokens
from data.candle_aggregator import MultiSymbolCandleAggregator
from data.historical_loader import HistoricalDataLoader
from data.instrument_resolver import instrument_resolver
from data.sector_peer_manager import get_sector_index_symbol
from data.time_utils import now_ist, now_ist_iso, now_ist_naive
from streaming.live_market_state import live_market_state
from streaming.live_signal_engine import live_signal_engine
from streaming.ssf_market_context import ssf_context_store
from streaming.ssf_one_minute_runtime import ssf_one_minute_runtime

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
        self.futures_token_to_symbol: Dict[int, str] = {}
        self.index_token_to_symbol: Dict[int, str] = {}
        self._history_ready_symbols: set[str] = set()
        self._history_lock = threading.Lock()

    def is_history_ready(
        self,
        symbol: str,
    ) -> bool:
        clean = str(symbol).strip().upper()
        with self._history_lock:
            return clean in self._history_ready_symbols

    def _warm_historical_state(
        self,
        token_to_symbol: Dict[int, str],
        kite_client: Any,
    ) -> None:
        """
        Seed the live in-memory candle state with real completed 15-minute
        history before/alongside the streaming session.

        Existing validated cache is reused. Missing history is fetched from
        Zerodha Kite through HistoricalDataLoader.

        No synthetic data is permitted.
        """
        now = now_ist_naive()

        for token, symbol in token_to_symbol.items():
            cache_path = (
                settings.base_dir
                / "data"
                / "cache"
                / f"{symbol}_15m.csv"
            )

            try:
                df = HistoricalDataLoader.load_or_refresh_intraday_cache(
                    kite_client=kite_client,
                    instrument_token=int(token),
                    cache_path=cache_path,
                    now=now,
                    lookback_days=45,
                    interval="15minute",
                )

                if df is None or df.empty:
                    logger.warning(
                        "[MarketStreamManager] No historical 15m data "
                        "available for %s",
                        symbol,
                    )
                    continue

                seeded = live_market_state.seed_historical_candles(
                    symbol=symbol,
                    candles=df,
                )

                with self._history_lock:
                    self._history_ready_symbols.add(
                        str(symbol).strip().upper()
                    )

                logger.info(
                    "[MarketStreamManager] Seeded %s historical "
                    "candles for %s",
                    seeded,
                    symbol,
                )

            except Exception as exc:
                logger.error(
                    "[MarketStreamManager] Historical warm-up failed "
                    "for %s (token=%s): %s",
                    symbol,
                    token,
                    exc,
                )

    def start_stream(
        self,
        token_to_symbol: Optional[Dict[int, str]] = None,
        kite_client: Optional[Any] = None,
        timeframe_minutes: int = 15,
    ) -> Dict[str, Any]:
        """
        Starts the KiteTicker WebSocket stream.

        The same authenticated Kite client is used for:
        - instrument/token resolution
        - historical peer-data fallback for Sector Impulse
        - other live strategy context requiring Kite REST data

        No order execution occurs here.
        """
        with self._lock:
            # Stop any existing stream before starting a new one.
            if self.kws is not None and self.state == StreamState.CONNECTED:
                self._stop_internal()

            # --------------------------------------------------------------
            # 1. Resolve the authenticated Kite client once.
            # --------------------------------------------------------------
            if kite_client is None:
                kite_client = get_active_kite()

            if kite_client is None:
                self.state = StreamState.ERROR
                self.last_error = (
                    "No active Zerodha Kite client available. "
                    "Please authenticate via Kite Connect."
                )
                raise RuntimeError(self.last_error)

            # --------------------------------------------------------------
            # 2. Resolve tokens using the authenticated Kite client.
            # --------------------------------------------------------------
            if not token_to_symbol:
                token_map = resolve_universe_tokens(
                    kite_client=kite_client
                )

                token_to_symbol = {
                    int(tok): sym
                    for sym, tok in token_map.items()
                    if tok
                }

            if not token_to_symbol:
                self.state = StreamState.ERROR
                self.last_error = (
                    "No valid instrument tokens available "
                    "to start market stream."
                )
                raise ValueError(self.last_error)

            # --------------------------------------------------------------
            # 3. Validate the saved session.
            # --------------------------------------------------------------
            session = get_saved_session()

            if not session:
                self.state = StreamState.ERROR
                self.last_error = (
                    "No active Zerodha Kite session found. "
                    "Please authenticate via Kite login."
                )
                raise RuntimeError(self.last_error)

            api_key = session.get("api_key")
            access_token = session.get("access_token")

            if not api_key or not access_token:
                self.state = StreamState.ERROR
                self.last_error = "Invalid session credentials."
                raise RuntimeError(self.last_error)

            self.token_to_symbol = dict(token_to_symbol)

            # Resolve nearest single-stock futures for SSF lead-lag
            self.futures_token_to_symbol = {}
            for sym in self.token_to_symbol.values():
                try:
                    fut_tok = instrument_resolver.find_nearest_single_stock_future(
                        sym, kite_client=kite_client
                    )
                    if fut_tok:
                        self.futures_token_to_symbol[int(fut_tok)] = sym
                except Exception as exc:
                    logger.debug(
                        "[MarketStreamManager] Failed to resolve futures token for %s: %s",
                        sym,
                        exc,
                    )

            # Resolve sector index tokens for SSF 30m sector returns
            self.index_token_to_symbol = {}
            for sym in self.token_to_symbol.values():
                try:
                    idx_sym = get_sector_index_symbol(sym)
                    if idx_sym:
                        idx_tok = instrument_resolver.resolve_token(
                            idx_sym, exchange="NSE", kite_client=kite_client
                        )
                        if idx_tok:
                            self.index_token_to_symbol[int(idx_tok)] = idx_sym
                except Exception as exc:
                    logger.debug(
                        "[MarketStreamManager] Failed to resolve index token for %s: %s",
                        sym,
                        exc,
                    )

            all_tokens = (
                list(self.token_to_symbol.keys())
                + list(self.futures_token_to_symbol.keys())
                + list(self.index_token_to_symbol.keys())
            )
            self.subscribed_token_count = len(all_tokens)
            self.state = StreamState.CONNECTING
            self.last_error = None

            # --------------------------------------------------------------
            # 4. Initialize live state.
            # --------------------------------------------------------------
            live_market_state.set_token_map(
                self.token_to_symbol
            )

            # Initialize 1-minute runtime for SSF regime and return tracking
            ssf_one_minute_runtime.initialize(
                symbols=list(self.token_to_symbol.values()),
                kite_client=kite_client,
                seed_history=True,
            )

            # --------------------------------------------------------------
            # 5. Candle-close callback keeps the SAME Kite client.
            # --------------------------------------------------------------
            def _on_candle_close(
                candle_dict: dict,
                vwap: float,
            ):
                with self._lock:
                    self.candle_count += 1

                live_market_state.update_candle_close(
                    candle_dict,
                    vwap,
                )

                symbol = str(
                    candle_dict.get("symbol", "")
                ).strip().upper()

                if not self.is_history_ready(symbol):
                    logger.info(
                        "[MarketStreamManager] Skipping strategy evaluation "
                        "for %s: historical warm-up not complete.",
                        symbol,
                    )
                    return

                live_signal_engine.on_candle_close(
                    candle_dict,
                    vwap,
                    kite_client=kite_client,
                )

            def _on_book_update(
                symbol: str,
                snapshot: Any,
            ):
                live_market_state.update_book_snapshot(
                    symbol,
                    snapshot,
                )
                # Route L5 depth directly to the persistent SSF runtime.
                # ORB / CPR / Dual-EMA / APEX / SIT are not called here;
                # they run at candle-close time via on_candle_close().
                live_signal_engine.on_book_update(
                    symbol=symbol,
                    snapshot=snapshot,
                )

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
                self.last_error = (
                    "kiteconnect package is not installed."
                )
                raise RuntimeError(self.last_error)

            kws = KiteTicker(
                api_key,
                access_token,
            )

            tokens_to_subscribe = list(all_tokens)

            def on_ticks(ws, ticks):
                now_ts = now_ist()

                with self._lock:
                    self.tick_count += (
                        len(ticks)
                        if isinstance(ticks, list)
                        else 1
                    )
                    self.last_tick_time = now_ts

                cash_ticks = []
                for t in (
                    ticks
                    if isinstance(ticks, list)
                    else [ticks]
                ):
                    tok = t.get(
                        "instrument_token"
                    )
                    if not tok:
                        continue
                    tok = int(tok)

                    # 1. Futures tick -> update SSF futures LTP & OI
                    if tok in self.futures_token_to_symbol:
                        fut_sym = self.futures_token_to_symbol[tok]
                        fut_ltp = t.get("last_price")
                        fut_oi = t.get("oi")
                        ssf_context_store.update_futures(
                            symbol=fut_sym,
                            fut_ltp=fut_ltp,
                            fut_oi=fut_oi,
                            timestamp=now_ts.replace(tzinfo=None),
                        )
                        continue

                    # 2. Sector index tick -> update 1m runtime
                    if tok in self.index_token_to_symbol:
                        ssf_one_minute_runtime.on_tick(t)
                        continue

                    # 3. Cash stock tick
                    sym = self.token_to_symbol.get(tok)
                    last_price = t.get("last_price")
                    vol_traded = t.get("volume_traded", t.get("volume", 0))

                    if (
                        sym
                        and last_price is not None
                    ):
                        live_market_state.update_tick(
                            symbol=sym,
                            price=float(last_price),
                            volume=int(vol_traded or 0),
                            timestamp=now_ts.replace(
                                tzinfo=None
                            ),
                            token=tok,
                        )

                    # Feed cash tick to 1m runtime for regime & stock returns
                    ssf_one_minute_runtime.on_tick(t)
                    cash_ticks.append(t)

                if self.aggregator and cash_ticks:
                    try:
                        self.aggregator.process_ticks(
                            cash_ticks
                        )
                    except Exception as e:
                        logger.error(
                            "[MarketStreamManager] "
                            f"Error processing ticks: {e}"
                        )

            def on_connect(ws, response):
                with self._lock:
                    self.state = StreamState.CONNECTED
                    self.last_connect_time = now_ist()
                ws.subscribe(tokens_to_subscribe)
                ws.set_mode(ws.MODE_FULL, tokens_to_subscribe)
                logger.info(
                    "[MarketStreamManager] KiteTicker connected in MODE_FULL — "
                    "subscribed %d instruments (%d cash, %d futures, %d indices).",
                    len(tokens_to_subscribe),
                    len(self.token_to_symbol),
                    len(self.futures_token_to_symbol),
                    len(self.index_token_to_symbol),
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

            warmup_thread = threading.Thread(
                target=self._warm_historical_state,
                args=(
                    dict(self.token_to_symbol),
                    kite_client,
                ),
                name="kite-history-warmup",
                daemon=True,
            )
            warmup_thread.start()

            kws.connect(
                threaded=True
            )

            logger.info(
                "[MarketStreamManager] "
                "KiteTicker stream connecting in background."
            )

            return {
                "status": "CONNECTING",
                "subscribed_tokens": len(
                    tokens_to_subscribe
                ),
                "symbols_count": len(
                    self.token_to_symbol
                ),
            }

    def _stop_internal(self) -> None:
        with self._history_lock:
            self._history_ready_symbols.clear()
        ssf_one_minute_runtime.stop()
        live_signal_engine.reset()
        self.futures_token_to_symbol.clear()
        self.index_token_to_symbol.clear()
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
