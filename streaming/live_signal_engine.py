"""
Live signal computation engine driven by streaming completed candles and book depth updates.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

import pandas as pd

from data.time_utils import now_ist_iso, now_ist_naive
from strategy.prediction_service import CandidatePrediction, prediction_service
from streaming.live_market_state import live_market_state

logger = logging.getLogger("streaming.live_signal_engine")


class LiveSignalEngine:
    """
    Computes and maintains the latest live strategy signals for subscribed instruments
    as 15-minute candles complete on the WebSocket stream.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._predictions: Dict[str, Dict[str, Any]] = {}

    def on_candle_close(
        self,
        candle_dict: Dict[str, Any],
        vwap: float,
        kite_client: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Invoked when a 15m candle closes for a streaming symbol.
        Evaluates ORB, CPR, Dual-EMA, APEX, Sector Impulse, and SSF-L5-SRM.
        """
        symbol = candle_dict.get("symbol")
        if not symbol:
            return None

        sym_state = live_market_state.get_symbol_state(symbol)
        token = sym_state.token if sym_state else None
        ltp = float(candle_dict.get("close", 0.0))
        book_snap = sym_state.book_snapshot if sym_state else None

        df_15m = live_market_state.get_candles_df(symbol)
        if df_15m.empty:
            # Fall back to single row if rolling buffer is not yet populated
            df_15m = pd.DataFrame([candle_dict])

        # Ensure datetime is datetime object and vwap is present
        if "datetime" in df_15m.columns:
            df_15m["datetime"] = pd.to_datetime(df_15m["datetime"])
        if "vwap" not in df_15m.columns:
            df_15m["vwap"] = vwap

        try:
            preds, consensus = prediction_service.evaluate_symbol(
                symbol=symbol,
                df_15m=df_15m,
                current_ltp=ltp,
                token=token,
                book_snapshot=book_snap,
                kite_client=kite_client,
            )

            result = {
                "symbol": symbol,
                "token": token,
                "ltp": ltp,
                "vwap": vwap,
                "predictions": {k: v.to_dict() for k, v in preds.items()},
                "consensus": consensus,
                "timestamp": now_ist_iso(),
            }

            with self._lock:
                self._predictions[symbol] = result

            logger.info(
                f"[LiveSignalEngine] Evaluated signals for {symbol}: "
                f"Consensus={consensus.get('direction')} ({consensus.get('label')})"
            )
            return result
        except Exception as e:
            logger.error(f"[LiveSignalEngine] Error evaluating signals for {symbol}: {e}")
            return None

    def get_prediction(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._predictions.get(symbol)

    def get_all_predictions(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return dict(self._predictions)

    def get_live_signals(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._predictions.values())

    def reset(self) -> None:
        with self._lock:
            self._predictions.clear()


live_signal_engine = LiveSignalEngine()
