"""
One place that runs the price-action / order-flow indicators for a symbol.

Before this module, SwingStructure, LiquiditySweep, FairValueGap, AnchoredVWAP
and OrderBookImbalance were implemented but never called by the live system.
build_indicator_context() runs them on the same completed candles the
strategies use and returns plain JSON-safe facts.

It is informational only: it never changes a strategy's signal, and any
indicator that fails or lacks data reports {"available": False, "reason": ...}
instead of raising.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, Optional

import pandas as pd

from backend.indicators.anchored_vwap import AnchoredVWAPEngine
from backend.indicators.fair_value_gap import FairValueGapEngine
from backend.indicators.liquidity_sweep import LiquiditySweepEngine
from backend.indicators.order_book_imbalance import OrderBookImbalanceEngine
from backend.indicators.swing_structure import SwingStructureEngine

_swing = SwingStructureEngine()
_sweep = LiquiditySweepEngine()
_fvg = FairValueGapEngine()
_avwap = AnchoredVWAPEngine()
_book = OrderBookImbalanceEngine()


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 4)
    return value


def _run(name: str, fn: Callable[[], Any]) -> Dict[str, Any]:
    try:
        out = _json_safe(fn())
        if isinstance(out, dict):
            out["available"] = True
            return out
        return {"available": True, "value": out}
    except Exception as exc:  # informational layer: never break evaluation
        return {"available": False, "reason": f"{name} failed: {type(exc).__name__}: {exc}"}


def build_indicator_context(
    df: Optional[pd.DataFrame],
    current_ltp: Optional[float] = None,
    book_snapshot: Optional[Any] = None,
) -> Dict[str, Any]:
    """Facts from the five price-action / order-flow indicators."""
    ctx: Dict[str, Any] = {}

    if df is None or df.empty or not {"datetime", "open", "high", "low", "close"}.issubset(df.columns):
        reason = "No completed candles available."
        for key in ("swing_structure", "liquidity_sweep", "fair_value_gap", "anchored_vwap"):
            ctx[key] = {"available": False, "reason": reason}
    else:
        ltp = float(current_ltp) if current_ltp else None
        ctx["swing_structure"] = _run("swing_structure", lambda: _swing.calculate_facts(df, current_ltp=ltp))
        ctx["liquidity_sweep"] = _run("liquidity_sweep", lambda: _sweep.detect_sweep(df))
        ctx["fair_value_gap"] = _run("fair_value_gap", lambda: _fvg.detect_fvgs(df, current_ltp=ltp))
        if "volume" in df.columns:
            ctx["anchored_vwap"] = _run(
                "anchored_vwap", lambda: _avwap.calculate_anchored_vwap(df, current_ltp=ltp)
            )
        else:
            ctx["anchored_vwap"] = {"available": False, "reason": "Candles have no volume column."}

    if book_snapshot is None:
        ctx["order_book"] = {"available": False, "reason": "No live Level-5 order book snapshot."}
    else:
        ctx["order_book"] = _run("order_book", lambda: _book.calculate_imbalance(book_snapshot))

    return ctx
