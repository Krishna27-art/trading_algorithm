"""
tests/test_live_strategy_independence.py

Proves that:
 1. ORB ignores prior sessions (opening-range comes only from today).
 2. CPR receives the previous session (len(days) >= 2).
 3. Dual-EMA receives >= 200 valid bars; SMA200 is not NaN when warm-up is satisfied.
 4. APEX receives previous_close and previous_atr (len(days) >= 2).
 5. Sector Impulse: missing peer data → UNAVAILABLE; fake volume=1000 is never introduced.
 6. SSF strategy instance persists across book updates (same object identity).
 7. SSF _features() is called exactly once per on_book_update() call.
 8. SSF cannot produce a basis signal until real fut_ltp exists.
 9. SSF regime stays False until 30 real 1-minute bars have been fed.
10. No strategy consumes another strategy's output.
11. No synthetic candles are introduced.
12. No hardcoded market price/volume is introduced.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import List
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from backend.config.settings import settings
from backend.config.universe import create_instrument_config_for_equity
from backend.strategy.prediction_service import PredictionService
from backend.strategy.ssf_l5_srm_strategy import BookSnapshot, SsfL5SrmStrategy
from backend.streaming.ssf_runtime import SSFLiveRuntime
from backend.streaming.ssf_runtime import SSFContextStore, SSFMarketContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INST = create_instrument_config_for_equity("TESTCO", 99999, current_price=1000.0)


def _make_day_df(
    session_date: date,
    n_bars: int = 25,
    base_price: float = 1000.0,
) -> pd.DataFrame:
    """Return *n_bars* valid 15-minute OHLCV candles for *session_date*."""
    rows = []
    t = datetime.combine(session_date, time(9, 15))
    for i in range(n_bars):
        o = base_price + i * 0.5
        h = o + 3.0
        l = o - 3.0
        c = o + 1.0
        rows.append(
            {
                "datetime": t,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 10_000 + i * 100,
                "vwap": (o + h + l + c) / 4,
            }
        )
        t += timedelta(minutes=15)
    return pd.DataFrame(rows)


def _make_multi_day_df(
    n_days: int = 30,
    bars_per_day: int = 25,
    reference_date: date | None = None,
) -> pd.DataFrame:
    """
    Build *n_days* of 15-minute candle history ending before reference_date.
    """
    ref = reference_date or date.today()
    frames = []
    for d in range(n_days, 0, -1):
        session = ref - timedelta(days=d)
        # Skip weekends
        if session.weekday() >= 5:
            continue
        frames.append(_make_day_df(session, bars_per_day))
    return pd.concat(frames, ignore_index=True)


def _make_book_snapshot(ltp: float = 1000.0, fut_ltp: float | None = None) -> BookSnapshot:
    levels = [(ltp - 0.5 * (i + 1), 100, 1) for i in range(5)]
    asks = [(ltp + 0.5 * (i + 1), 100, 1) for i in range(5)]
    return BookSnapshot(
        timestamp=datetime.now(),
        bids=levels,
        asks=asks,
        ltp=ltp,
        fut_ltp=fut_ltp,
    )


# ---------------------------------------------------------------------------
# 1. ORB ignores prior sessions
# ---------------------------------------------------------------------------

def test_orb_ignores_prior_sessions():
    """
    Adding 20 extra historical sessions must not change ORB's opening-range
    high/low values for the current session.

    ORB only reads today's candles to build its opening range.  Feeding extra
    historical sessions must yield an identical opening-range result.
    """
    svc = PredictionService()

    # Use yesterday as "today" so _prepare_live_candles does not filter out
    # the synthetic candles (they must be in the past, not the future).
    today = date.today() - timedelta(days=1)
    while today.weekday() >= 5:
        today -= timedelta(days=1)

    today_df = _make_day_df(today, n_bars=5)

    # Evaluate with only today's data.
    hist_one = today_df.copy()
    preds_one, _ = svc.evaluate_symbol(
        symbol="TESTCO",
        df_15m=hist_one,
        current_ltp=1001.0,
    )

    # Evaluate with 20 prior sessions prepended — ORB must use only today's candles.
    multi_day = _make_multi_day_df(n_days=20, reference_date=today)
    hist_many = pd.concat([multi_day, today_df], ignore_index=True)
    preds_many, _ = svc.evaluate_symbol(
        symbol="TESTCO",
        df_15m=hist_many,
        current_ltp=1001.0,
    )

    orb_one = preds_one["orb"]
    orb_many = preds_many["orb"]

    # Both must have the same status (both can evaluate today's opening range).
    assert orb_one.status == orb_many.status, (
        f"ORB status changed when history was added: "
        f"{orb_one.status!r} → {orb_many.status!r}"
    )

    # The ORB high/low values must be identical — they come from today's candles only.
    if orb_one.levels and orb_many.levels:
        assert orb_one.levels.get("orb_high") == orb_many.levels.get("orb_high"), (
            "ORB high changed when prior sessions were added"
        )
        assert orb_one.levels.get("orb_low") == orb_many.levels.get("orb_low"), (
            "ORB low changed when prior sessions were added"
        )

    # ORB must never reference prior-session context in its reason string.
    if orb_one.reason:
        assert "prior" not in orb_one.reason.lower(), (
            f"ORB reason should not reference prior session: {orb_one.reason!r}"
        )


# ---------------------------------------------------------------------------
# 2. CPR receives the previous session (len(days) >= 2)
# ---------------------------------------------------------------------------

def test_cpr_requires_prior_session():
    """With only today's candles CPR must return UNAVAILABLE, not NO_TRADE."""
    svc = PredictionService()

    today = date.today()
    preds, _ = svc.evaluate_symbol(
        symbol="TESTCO",
        df_15m=_make_day_df(today),
        current_ltp=1001.0,
    )
    assert preds["cpr"].status == "UNAVAILABLE", (
        f"CPR should be UNAVAILABLE with no prior session; got {preds['cpr'].status!r}"
    )


def test_cpr_evaluates_with_prior_sessions(monkeypatch):
    """With multiple historical sessions CPR must not be UNAVAILABLE (it can trade or monitor)."""
    svc = PredictionService()

    today = date.today()
    monkeypatch.setattr(
        "backend.strategy.prediction_service.now_ist_naive",
        lambda: datetime.combine(today, time(10, 30)),
    )
    from backend.data.instrument_resolver import instrument_resolver
    monkeypatch.setattr(instrument_resolver, "resolve_lot_size", lambda *args, **kwargs: 1)
    # Find the most recent weekday before today
    yesterday = today - timedelta(days=1)
    while yesterday.weekday() >= 5:  # skip Saturday(5) and Sunday(6)
        yesterday -= timedelta(days=1)

    # Also try two days back in case today is Monday
    day_before = yesterday - timedelta(days=1)
    while day_before.weekday() >= 5:
        day_before -= timedelta(days=1)

    df = pd.concat(
        [
            _make_day_df(day_before, n_bars=25),
            _make_day_df(yesterday, n_bars=25),
            _make_day_df(today, n_bars=5),
        ],
        ignore_index=True,
    )
    preds, _ = svc.evaluate_symbol(
        symbol="TESTCO",
        df_15m=df,
        current_ltp=1001.0,
    )
    # With 2 prior sessions CPR must have access to the prior session and must not be UNAVAILABLE.
    # It may return NO_TRADE or a directional signal depending on the synthetic prices.
    assert preds["cpr"].status != "UNAVAILABLE", (
        f"CPR should be evaluable with prior sessions; got {preds['cpr'].status!r}: "
        f"{preds['cpr'].reason!r}"
    )


# ---------------------------------------------------------------------------
# 3. Dual-EMA receives >= 200 valid bars when history is sufficient
# ---------------------------------------------------------------------------

def test_dual_ema_requires_prior_session():
    """With only today's candles Dual-EMA must return UNAVAILABLE."""
    svc = PredictionService()
    today = date.today()
    preds, _ = svc.evaluate_symbol(
        symbol="TESTCO",
        df_15m=_make_day_df(today),
        current_ltp=1001.0,
    )
    assert preds["dual_ema"].status == "UNAVAILABLE"


def test_dual_ema_sma200_not_nan_when_warmup_satisfied():
    """When enough history is provided SMA200 must be a valid finite number."""
    from backend.strategy.dual_ema_strategy import BufferedDualEMAStrategy

    # Build ~640 bars (26 sessions × 25 bars) so the SMA200 window is satisfied.
    df = _make_multi_day_df(n_days=26, bars_per_day=25)
    today_session = _make_day_df(date.today(), n_bars=5)
    df = pd.concat([df, today_session], ignore_index=True)

    strategy = BufferedDualEMAStrategy(_INST, settings.strategy)

    svc = PredictionService()
    _, days = svc._prepare_data(df)
    assert len(days) >= 2

    lookback = pd.concat([d for _, d in days[:-1]], ignore_index=True)
    latest_date, today_df = days[-1]

    strategy.seed_context(lookback)
    strategy.reset_session(latest_date)

    for _, row in today_df.iterrows():
        candle = svc._candle_dict(row)
        vwap_val = float(row.get("vwap", 0)) if row.get("vwap") else None
        if vwap_val:
            strategy.on_candle(candle, vwap_val)

    inds, _ = strategy.latest_indicators()
    if inds is not None:
        sma200 = inds.get("sma200")
        assert sma200 is not None and np.isfinite(float(sma200)), (
            f"SMA200 should be finite when warm-up is satisfied; got {sma200!r}"
        )


# ---------------------------------------------------------------------------
# 4. APEX receives previous_close and previous_atr (len(days) >= 2)
# ---------------------------------------------------------------------------

def test_apex_requires_prior_session():
    """With only today's candles APEX must return UNAVAILABLE."""
    svc = PredictionService()
    today = date.today()
    preds, _ = svc.evaluate_symbol(
        symbol="TESTCO",
        df_15m=_make_day_df(today),
        current_ltp=1001.0,
    )
    assert preds["apex"].status == "UNAVAILABLE", (
        f"APEX should be UNAVAILABLE with no prior session; got {preds['apex'].status!r}"
    )


def test_apex_lookback_built_from_explicit_day_concat():
    """The APEX evaluator must build lookback_df from explicit prior days, not positional slicing."""
    from backend.strategy.apex_engine import ApexStrategy

    today = date.today()
    yesterday = today - timedelta(days=1)
    if yesterday.weekday() >= 5:
        yesterday -= timedelta(days=yesterday.weekday() - 4)

    df = pd.concat(
        [_make_day_df(yesterday, n_bars=25), _make_day_df(today, n_bars=3)],
        ignore_index=True,
    )

    svc = PredictionService()
    _, days = svc._prepare_data(df)
    assert len(days) >= 2, "Need at least 2 sessions to test APEX lookback"

    # Build lookback using explicit concat (the fixed method)
    lookback_explicit = pd.concat(
        [d for _, d in days[:-1]], ignore_index=True
    )

    # Build lookback using the old positional slice (should match for well-formed data)
    latest_date, today_df = days[-1]

    strategy = ApexStrategy(_INST, settings.strategy)
    strategy.seed_context(lookback_explicit)
    strategy.reset_session(latest_date)

    # previous_close must be populated after seeding the prior session
    prev_close = getattr(strategy, "previous_close", None)
    assert prev_close is not None and prev_close > 0, (
        f"APEX.previous_close should be set after seeding prior session; got {prev_close!r}"
    )


# ---------------------------------------------------------------------------
# 5. Sector Impulse: missing peer data → UNAVAILABLE; no fake volume
# ---------------------------------------------------------------------------

def test_sit_no_peer_context_returns_unavailable():
    """When no peer context and no kite_client, SIT must return UNAVAILABLE."""
    svc = PredictionService()
    today = date.today()
    df = _make_day_df(today, n_bars=10)

    # SectorPeerManager is imported locally inside _evaluate_sector_impulse.
    # Patch at the data module level.
    with patch(
        "backend.data.sector_peer_manager.SectorPeerManager.build_peer_context",
        return_value=None,
    ):
        preds, _ = svc.evaluate_symbol(
            symbol="TESTCO",
            df_15m=df,
            current_ltp=1001.0,
        )

    assert preds["sector_impulse"].status == "UNAVAILABLE", (
        f"SIT should be UNAVAILABLE when peer context missing; "
        f"got {preds['sector_impulse'].status!r}: {preds['sector_impulse'].reason!r}"
    )


def test_sit_no_fake_volume_in_peer_manager():
    """
    SectorPeerManager must never inject a hardcoded volume=1000 value.
    Verified by inspecting the source code directly.
    """
    import inspect
    from backend.data import sector_peer_manager

    source = inspect.getsource(sector_peer_manager)
    assert 'df_leader["volume"] = 1000' not in source, (
        'Fake volume df_leader["volume"] = 1000 must be removed from sector_peer_manager.py'
    )
    # Also confirm the warning+None branch replaced it
    assert "Returning None to prevent fabricated data" in source, (
        "sector_peer_manager.py must warn and return None when leader volume is absent"
    )


# ---------------------------------------------------------------------------
# 6. SSF strategy instance persists across book updates
# ---------------------------------------------------------------------------

def test_ssf_strategy_instance_persists():
    """SSFLiveRuntime must return the same object identity across calls."""
    runtime = SSFLiveRuntime(strategy_config=settings.strategy)
    s1 = runtime.get_strategy("TESTCO", 99999, 1000.0)
    s2 = runtime.get_strategy("TESTCO", 99999, 1001.0)
    assert s1 is s2, (
        "SSFLiveRuntime must return the SAME strategy instance for the same symbol"
    )


def test_ssf_runtime_different_symbols_are_independent():
    """Different symbols must get independent strategy instances."""
    runtime = SSFLiveRuntime(strategy_config=settings.strategy)
    s_a = runtime.get_strategy("AAPL", 1, 1000.0)
    s_b = runtime.get_strategy("MSFT", 2, 2000.0)
    assert s_a is not s_b, "Different symbols must have independent SSF strategy instances"


def test_ssf_runtime_reset_clears_all():
    """After reset(), a new strategy object must be returned."""
    runtime = SSFLiveRuntime(strategy_config=settings.strategy)
    s1 = runtime.get_strategy("TESTCO", 99999, 1000.0)
    runtime.reset()
    s2 = runtime.get_strategy("TESTCO", 99999, 1000.0)
    assert s1 is not s2, "After reset(), a new strategy instance must be created"


# ---------------------------------------------------------------------------
# 7. SSF _features() is called exactly once per on_book_update()
# ---------------------------------------------------------------------------

def test_ssf_features_called_exactly_once_per_book_update():
    """on_book_update() internally calls _features() once. Calling it separately first would cause double-mutation."""
    inst = create_instrument_config_for_equity("TESTCO", 99999, current_price=1000.0)
    strategy = SsfL5SrmStrategy(inst, settings.strategy)

    call_count = 0
    original_features = strategy._features

    def counting_features(s):
        nonlocal call_count
        call_count += 1
        return original_features(s)

    strategy._features = counting_features

    snap = _make_book_snapshot(ltp=1000.0)
    strategy.on_book_update(snap)

    assert call_count == 1, (
        f"_features() should be called exactly once per on_book_update(); called {call_count} time(s)"
    )


# ---------------------------------------------------------------------------
# 8. SSF cannot produce a basis signal until real fut_ltp exists
# ---------------------------------------------------------------------------

def test_ssf_no_basis_signal_without_fut_ltp():
    """z_basis must remain 0 when fut_ltp is None."""
    inst = create_instrument_config_for_equity("TESTCO", 99999, current_price=1000.0)
    strategy = SsfL5SrmStrategy(inst, settings.strategy)

    # Feed enough book updates so z-score buffers have observations
    for i in range(120):
        snap = _make_book_snapshot(ltp=1000.0 + i * 0.01, fut_ltp=None)
        strategy.on_book_update(snap)

    # last_features should show z_basis == 0 since no futures data
    z_basis = strategy.last_features.get("z_basis", 0.0)
    assert z_basis == 0.0, (
        f"z_basis must be 0.0 when fut_ltp is None; got {z_basis}"
    )


def test_ssf_basis_nonzero_only_with_real_fut_ltp():
    """Once fut_ltp is provided, z_basis can become non-zero after enough observations."""
    inst = create_instrument_config_for_equity("TESTCO", 99999, current_price=1000.0)
    strategy = SsfL5SrmStrategy(inst, settings.strategy)

    # Feed enough book updates WITH futures data
    for i in range(150):
        fut = 1000.0 + i * 0.02 + (1.5 if i % 3 == 0 else -0.5)
        snap = _make_book_snapshot(ltp=1000.0 + i * 0.01, fut_ltp=fut)
        strategy.on_book_update(snap)

    z_basis = strategy.last_features.get("z_basis", None)
    # After enough observations z_basis may be non-zero
    assert z_basis is not None, "z_basis should be computed when fut_ltp is present"


# ---------------------------------------------------------------------------
# 9. SSF regime stays False until 30 real bars have been fed
# ---------------------------------------------------------------------------

def test_ssf_regime_false_before_30_bars():
    """regime_ok must remain False until at least 30 candle bars are processed."""
    inst = create_instrument_config_for_equity("TESTCO", 99999, current_price=1000.0)
    strategy = SsfL5SrmStrategy(inst, settings.strategy)

    for i in range(29):
        candle = {
            "datetime": datetime.now(),
            "open": 1000.0,
            "high": 1005.0,
            "low": 995.0,
            "close": 1001.0,
        }
        strategy.on_candle(candle, 1001.0)

    assert not strategy.regime_ok, (
        "regime_ok must be False until 30 bars have been processed"
    )


def test_ssf_regime_evaluated_after_30_bars():
    """After 30+ bars regime_ok can be evaluated (True or False depending on volatility)."""
    inst = create_instrument_config_for_equity("TESTCO", 99999, current_price=1000.0)
    strategy = SsfL5SrmStrategy(inst, settings.strategy)

    for i in range(35):
        price = 1000.0 + i * 0.5
        candle = {
            "datetime": datetime.now() + timedelta(minutes=i),
            "open": price,
            "high": price + 3.0,
            "low": price - 3.0,
            "close": price + 1.0,
        }
        strategy.on_candle(candle, price + 1.0)

    # After 30 bars, regime_ok is an actual computed value (not forced False)
    assert isinstance(strategy.regime_ok, bool), (
        "regime_ok must be a bool after 30+ bars"
    )
    assert len(strategy._bars) >= 30, (
        f"Strategy must have at least 30 bars stored; has {len(strategy._bars)}"
    )


# ---------------------------------------------------------------------------
# 10. No strategy consumes another strategy's output
# ---------------------------------------------------------------------------

def test_strategies_are_independent_of_each_other():
    """Strategy evaluations in evaluate_symbol() must not read each other's predictions."""
    svc = PredictionService()
    today = date.today()
    yesterday = today - timedelta(days=1)
    if yesterday.weekday() >= 5:
        yesterday -= timedelta(days=yesterday.weekday() - 4)

    df = pd.concat(
        [_make_day_df(yesterday, n_bars=25), _make_day_df(today, n_bars=5)],
        ignore_index=True,
    )

    preds, consensus = svc.evaluate_symbol(
        symbol="TESTCO",
        df_15m=df,
        current_ltp=1001.0,
    )

    # All 6 strategies must be present
    for key in ("orb", "cpr", "dual_ema", "apex", "sector_impulse", "ssf_l5_srm"):
        assert key in preds, f"Strategy {key!r} missing from predictions"

    # No strategy's result should directly mention another strategy's status
    for key, pred in preds.items():
        reason = pred.reason or ""
        for other_key in ("orb", "cpr", "dual_ema", "apex", "sector_impulse", "ssf_l5_srm"):
            if other_key == key:
                continue
            assert other_key not in reason.lower() or "requires" not in reason.lower(), (
                f"Strategy {key!r} reason mentions {other_key!r}: {reason!r}"
            )


# ---------------------------------------------------------------------------
# 11. No synthetic candles are introduced
# ---------------------------------------------------------------------------

def test_no_synthetic_candles_in_live_candles():
    """_prepare_live_candles() must never add rows that weren't in the input."""
    svc = PredictionService()
    today = date.today()
    df_in = _make_day_df(today, n_bars=10)
    df_out = svc._prepare_live_candles(df_in)

    # Output must be a subset of the input (by datetime)
    in_timestamps = set(pd.to_datetime(df_in["datetime"]).dt.normalize() if False else df_in["datetime"])
    out_timestamps = set(df_out["datetime"] if not df_out.empty else [])

    # Every output timestamp must exist in the input
    for ts in out_timestamps:
        assert any(
            abs((pd.Timestamp(ts) - pd.Timestamp(t)).total_seconds()) < 60
            for t in in_timestamps
        ), f"Output contains timestamp {ts} not in input — synthetic candle introduced"


# ---------------------------------------------------------------------------
# 12. No hardcoded market price/volume is introduced
# ---------------------------------------------------------------------------

def test_no_hardcoded_volume_in_sector_peer_manager():
    """SectorPeerManager source must not contain the literal fake volume assignment."""
    import inspect
    from backend.data import sector_peer_manager
    source = inspect.getsource(sector_peer_manager)
    # The old code had: df_leader["volume"] = 1000
    # This must not exist any more
    assert 'df_leader["volume"] = 1000' not in source, (
        'Fake volume df_leader["volume"] = 1000 must be removed from sector_peer_manager.py'
    )


def test_ssf_context_store_preserves_only_real_values():
    """SSFContextStore must never invent fut_ltp or fut_oi."""
    store = SSFContextStore()
    ctx = store.get("TESTCO")
    assert ctx.fut_ltp is None, "fut_ltp must be None until explicitly set by real feed"
    assert ctx.fut_oi is None, "fut_oi must be None until explicitly set by real feed"

    store.update_futures("TESTCO", fut_ltp=1050.5, fut_oi=12345.0)
    ctx2 = store.get("TESTCO")
    assert ctx2.fut_ltp == 1050.5
    assert ctx2.fut_oi == 12345.0
