from datetime import datetime, timedelta

import pandas as pd

from backend.indicators.market_context import build_indicator_context
from backend.data.models import BookSnapshot


def _candles(days: int = 3) -> pd.DataFrame:
    rows, price = [], 100.0
    start = datetime(2026, 10, 1, 9, 15)
    for d in range(days):
        for k in range(25):
            t = start + timedelta(days=d, minutes=15 * k)
            o = price
            c = o + (0.4 if (k // 3) % 2 == 0 else -0.35)
            rows.append(dict(symbol="RELIANCE", datetime=t, open=o, high=max(o, c) + 0.2,
                             low=min(o, c) - 0.2, close=c, volume=1000 + 10 * k))
            price = c
    return pd.DataFrame(rows)


KEYS = {"swing_structure", "liquidity_sweep", "fair_value_gap", "anchored_vwap", "order_book"}


def test_all_indicators_report_on_real_candles():
    df = _candles()
    ctx = build_indicator_context(df, current_ltp=float(df.close.iloc[-1]))
    assert set(ctx) == KEYS
    for key in KEYS - {"order_book"}:
        assert ctx[key]["available"] is True, ctx[key]
    assert ctx["swing_structure"]["pdh"] is not None
    assert ctx["order_book"]["available"] is False  # no book supplied


def test_order_book_imbalance_with_snapshot():
    book = BookSnapshot(timestamp=datetime(2026, 10, 3, 10, 0), ltp=100.0,
                        bids=[(99.9, 5000, 10), (99.8, 4000, 8)],
                        asks=[(100.1, 1000, 3), (100.2, 900, 2)])
    ctx = build_indicator_context(_candles(), 100.0, book)
    assert ctx["order_book"]["available"] is True
    assert ctx["order_book"]["pressure"] == "BULLISH"


def test_empty_data_fails_closed_without_raising():
    ctx = build_indicator_context(pd.DataFrame(), None, None)
    assert set(ctx) == KEYS
    assert all(v["available"] is False for v in ctx.values())
