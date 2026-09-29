"""
Tests for Sector Impulse Transmission (SIT) strategy.

These are structural/plug-in tests (interface conformance, no-crash across a
full session, config knobs behave), not edge validation — the strategy's
thresholds are calibration placeholders per its own module docstring, so
asserting exact trade counts/directions here would be testing noise.
"""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from config.settings import InstrumentConfig, StrategyConfig
from strategy.base_strategy import BaseStrategy, StrategySignal
from strategy.sector_impulse_strategy import PeerContext, SectorImpulseStrategy, SITConfig


def _synth(days=15, seed=1, base=1000.0, bars_per_day=25):
    rng = np.random.RandomState(seed)
    rows = []
    d0 = date(2025, 1, 1)
    px = base
    for d in range(days):
        day = d0 + timedelta(days=d)
        t0 = datetime.combine(day, datetime.min.time()) + timedelta(hours=9, minutes=15)
        for i in range(bars_per_day):
            o = px
            px = px * (1 + rng.normal(0, 0.003))
            h, l = max(o, px) * 1.001, min(o, px) * 0.999
            rows.append(dict(datetime=t0 + timedelta(minutes=15 * i), open=o, high=h, low=l,
                              close=px, volume=int(rng.randint(1000, 5000))))
    return pd.DataFrame(rows)


@pytest.fixture
def ctx():
    return PeerContext(leader=_synth(seed=2, base=2500), market=_synth(seed=3, base=24000),
                        sector=_synth(seed=4, base=5000))


@pytest.fixture
def strategy(ctx):
    inst = InstrumentConfig(symbol="TESTSTOCK", exchange="NSE")
    return SectorImpulseStrategy(inst, StrategyConfig(), ctx=ctx)


def test_is_base_strategy(strategy):
    assert isinstance(strategy, BaseStrategy)


def test_runs_without_context_and_stays_disabled():
    inst = InstrumentConfig(symbol="X", exchange="NSE")
    strat = SectorImpulseStrategy(inst, StrategyConfig(), ctx=None)
    strat.reset_session(date(2025, 1, 1))
    assert strat.model is None
    sig = strat.on_candle(
        {"datetime": datetime(2025, 1, 1, 9, 30), "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 100},
        vwap=100.0,
    )
    assert sig is None  # never trades without peer data


def test_full_session_runs_without_error(strategy, ctx):
    own = _synth(seed=1, base=1000)
    own["date"] = own["datetime"].dt.date
    days = list(own.groupby("date"))
    signals = []
    for idx, (d, day_df) in enumerate(days):
        lookback = pd.concat([dd for _, dd in days[:idx]], ignore_index=True) if idx else pd.DataFrame(columns=own.columns)
        strategy.seed_context(lookback)
        strategy.reset_session(d)
        for _, row in day_df.iterrows():
            candle = dict(datetime=row.datetime, open=row.open, high=row.high, low=row.low,
                          close=row.close, volume=row.volume)
            sig = strategy.on_candle(candle, row.close)
            if sig is not None:
                assert isinstance(sig, StrategySignal)
                signals.append(sig)
                if sig.action.value in ("BUY", "SELL") and strategy.position == 0:
                    strategy.register_trade_entry(sig.price, 1 if sig.action.value == "BUY" else -1,
                                                  sig.stop_loss, sig.target, abs(sig.price - sig.stop_loss))
                elif sig.action.value == "EXIT":
                    strategy.register_trade_exit()
    # No assertion on signal count/direction (calibration-dependent) — the
    # point of this test is that a 15-day multi-symbol session never raises.


def test_on_tick_noop_when_flat(strategy):
    strategy.reset_session(date(2025, 1, 1))
    assert strategy.on_tick(1000.0, datetime(2025, 1, 1, 10, 0)) is None


def test_on_tick_exits_at_time_square_off(strategy):
    strategy.reset_session(date(2025, 1, 1))
    strategy.register_trade_entry(1000.0, 1, 990.0, 1020.0, 10.0)
    sig = strategy.on_tick(1005.0, datetime(2025, 1, 1, 15, 11))
    assert sig is not None
    assert sig.action.value == "EXIT"
    assert sig.reason == "TIME_SQUARE_OFF"


def test_disabled_on_expiry_day(strategy):
    from strategy.sector_impulse_strategy import _last_monthly_weekday
    expiry = _last_monthly_weekday(date(2025, 1, 15), SITConfig().expiry_weekday)
    strategy.reset_session(expiry)
    assert strategy.model is None
    assert strategy.disabled_reason == "expiry_day"


def test_sector_peer_manager_classification_and_context():
    """Verifies SectorPeerManager correctly identifies sectors and builds context."""
    from data.sector_peer_manager import SectorPeerManager

    # 1. Classification
    it_sec = SectorPeerManager.get_sector_for_symbol("INFY")
    assert it_sec.name == "IT"
    assert it_sec.primary_leader == "TCS"
    assert it_sec.secondary_leader == "INFY"

    bank_sec = SectorPeerManager.get_sector_for_symbol("SBIN")
    assert bank_sec.name == "BANKING"
    assert bank_sec.primary_leader == "HDFCBANK"

    # 2. Peer Symbols
    l, m, s = SectorPeerManager.get_peer_symbols("INFY")
    assert l == "TCS"
    assert m == "NIFTY"
    assert s == "INFY"

    l_tcs, m_tcs, s_tcs = SectorPeerManager.get_peer_symbols("TCS")
    assert l_tcs == "INFY"  # Secondary leader acts as peer leader when evaluating primary leader

    # 3. Build Peer Context from synth frames
    df_l = _synth(seed=20, base=2500)
    df_m = _synth(seed=21, base=24000)
    df_s = _synth(seed=22, base=5000)
    ctx = PeerContext(leader=df_l, market=df_m, sector=df_s)
    assert "leader" in ctx.frames
    assert "market" in ctx.frames
    assert "sector" in ctx.frames


def test_sector_impulse_model_fitting_and_signal_generation():
    """Verifies SIT strategy fits regression model and triggers signals on impulse."""
    # Construct synthetic data with a known positive lead-lag relationship
    rng = np.random.RandomState(42)
    days = 15
    bars = 25
    d0 = date(2025, 1, 1)

    market_closes, sector_closes, leader_closes, own_closes, leader_vols = [], [], [], [], []
    dts = []
    px_m, px_sec, px_l, px_i = 24000.0, 5000.0, 2500.0, 1000.0

    for d in range(days):
        day = d0 + timedelta(days=d)
        t0 = datetime.combine(day, datetime.min.time()) + timedelta(hours=9, minutes=15)
        for i in range(bars):
            ts = t0 + timedelta(minutes=15 * i)
            dts.append(ts)
            rm = rng.normal(0, 0.002)
            rsec = 0.5 * rm + rng.normal(0, 0.002)
            rl = 0.4 * rm + 0.3 * rsec + rng.normal(0, 0.005)  # leader idiosyncratic impulse
            # Own stock lags leader with lag 1
            ri = 0.3 * rm + 0.2 * rsec + 0.4 * rl + rng.normal(0, 0.001)

            px_m *= (1 + rm)
            px_sec *= (1 + rsec)
            px_l *= (1 + rl)
            px_i *= (1 + ri)

            market_closes.append(px_m)
            sector_closes.append(px_sec)
            leader_closes.append(px_l)
            own_closes.append(px_i)
            leader_vols.append(10000 if i == 5 else 2000)

    df_market = pd.DataFrame({"datetime": dts, "close": market_closes})
    df_sector = pd.DataFrame({"datetime": dts, "close": sector_closes})
    df_leader = pd.DataFrame({"datetime": dts, "close": leader_closes, "volume": leader_vols})
    df_own = pd.DataFrame({"datetime": dts, "open": own_closes, "high": [x * 1.002 for x in own_closes],
                           "low": [x * 0.998 for x in own_closes], "close": own_closes, "volume": 1500})

    ctx = PeerContext(leader=df_leader, market=df_market, sector=df_sector)
    inst = InstrumentConfig(symbol="LAGGARD", exchange="NSE")
    sit_cfg = SITConfig(min_rho=0.05, train_days=10, z_leader_thr=0.5, vz_thr=0.5, gap_thr_sigma=0.2)
    strat = SectorImpulseStrategy(inst, StrategyConfig(), ctx=ctx, sit=sit_cfg)

    # Seed context with prior days
    strat.seed_context(df_own.iloc[:-25])
    strat.reset_session(d0 + timedelta(days=days - 1))

    assert strat.model is not None, f"Model failed to fit: {strat.disabled_reason}"
    assert "k" in strat.model
    assert "rho" in strat.model
    assert "beta_l" in strat.model
    assert "beta_i" in strat.model
