"""
Tests for ResidualMomentumStrategy (NSE-RM-100) and VRPHarvestStrategy
(NSE-VRP-INDEX).

The one that matters most is test_no_lookahead_in_scores: if a future price
can leak into a score, every other number in the backtest is fiction.
"""

from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from strategy.portfolio_base import MarketRegime, OrderSide
from strategy.residual_momentum import ResidualMomentumConfig, ResidualMomentumStrategy
from strategy.vrp_index import VRPConfig, VRPHarvestStrategy, bs_delta, parkinson_volatility
from risk.portfolio_costs import DeliveryCostCalculator, IndexOptionsCostCalculator


# --------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------- #
@pytest.fixture
def panel():
    """400 sessions, 80 symbols. Symbols 0-9 get an idiosyncratic drift over
    the RECENT half of the sample only.

    That detail matters: alpha is fitted over the same 252-day window the
    residuals are drawn from, so a drift present across the whole window is
    absorbed into alpha and leaves residuals near zero. The engine scores
    recent idiosyncratic strength relative to a stock's own one-year model,
    which is exactly what Blitz/Huij/Martens specify -- so the fixture has
    to inject the drift recently for the ranking to pick it up."""
    rng = np.random.default_rng(7)
    n, m = 400, 80
    dates = pd.bdate_range("2022-01-03", periods=n)
    symbols = [f"STK{i:02d}" for i in range(m)]

    market = np.cumprod(1 + rng.normal(0.0004, 0.009, n)) * 18000.0
    index_close = pd.Series(market, index=dates)
    mkt_ret = pd.Series(market, index=dates).pct_change().fillna(0.0).to_numpy()

    closes = {}
    for i, s in enumerate(symbols):
        beta = 0.7 + (i % 10) * 0.08
        idio = rng.normal(0.0, 0.011, n)
        if i < 10:
            idio[-160:-10] += 0.0030   # recent-only idiosyncratic drift
        ret = beta * mkt_ret + idio
        closes[s] = 500.0 * np.cumprod(1 + ret)
    closes = pd.DataFrame(closes, index=dates)
    highs = closes * 1.012
    lows = closes * 0.988
    volumes = pd.DataFrame(2_000_000.0, index=dates, columns=symbols)
    return closes, highs, lows, volumes, index_close, symbols


@pytest.fixture
def rm():
    return ResidualMomentumStrategy(ResidualMomentumConfig())


# --------------------------------------------------------------------- #
# NSE-RM-100
# --------------------------------------------------------------------- #
def test_rebalance_cadence_is_alternate_fridays(rm):
    anchor = rm.anchor_date
    assert rm.is_rebalance_day(anchor)
    assert not rm.is_rebalance_day(anchor + timedelta(days=7))
    assert rm.is_rebalance_day(anchor + timedelta(days=14))
    assert not rm.is_rebalance_day(anchor + timedelta(days=13))  # Thursday


def test_daily_risk_free_compounds_to_annual(rm):
    daily = rm.daily_risk_free(6.5)
    assert (1 + daily) ** 252 == pytest.approx(1.065, rel=1e-6)


def test_scores_rank_idiosyncratic_winners_first(rm, panel):
    closes, _, _, _, index_close, symbols = panel
    rf = pd.Series(rm.daily_risk_free(6.5), index=closes.index)
    scores = rm.compute_scores(closes, index_close, rf, symbols)
    top10 = set(scores.head(10)["symbol"])
    drifters = {f"STK{i:02d}" for i in range(10)}
    assert len(top10 & drifters) >= 6
    assert scores["rm_z"].abs().max() <= 3.0 + 1e-9


def test_no_lookahead_in_scores(rm, panel):
    """Scores computed through t-1 must not change when future rows are
    altered. Mutate everything after the cutoff and re-score."""
    closes, _, _, _, index_close, symbols = panel
    cutoff = 300
    rf = pd.Series(rm.daily_risk_free(6.5), index=closes.index)

    hist_c = closes.iloc[:cutoff]
    hist_i = index_close.iloc[:cutoff]
    base = rm.compute_scores(hist_c, hist_i, rf.iloc[:cutoff], symbols)

    poisoned = closes.copy()
    poisoned.iloc[cutoff:] *= 5.0
    poisoned_idx = index_close.copy()
    poisoned_idx.iloc[cutoff:] *= 5.0
    after = rm.compute_scores(
        poisoned.iloc[:cutoff], poisoned_idx.iloc[:cutoff], rf.iloc[:cutoff], symbols
    )
    pd.testing.assert_frame_equal(base, after)


def test_weights_respect_cap_floor_and_sum_to_one(rm, panel):
    closes, highs, lows, volumes, index_close, symbols = panel
    plan = rm.generate_plan(
        as_of=date(2023, 6, 2), closes=closes, highs=highs, lows=lows,
        volumes=volumes, index_close=index_close, universe=symbols,
        capital=1_000_000.0,
    )
    assert not plan.skipped
    assert len(plan.targets) <= rm.config.portfolio_size
    gross = plan.gross_exposure
    total = sum(t.weight for t in plan.targets)
    assert total == pytest.approx(gross, rel=1e-6)
    # With fewer than 1/max_weight names the cap widens to the nearest
    # arithmetically feasible value — see _apply_caps.
    cap = max(rm.config.max_weight, 1.0 / len(plan.targets))
    floor = min(rm.config.min_weight, 1.0 / len(plan.targets))
    for t in plan.targets:
        assert t.weight <= cap * gross * (1 + 1e-6)
        assert t.weight >= floor * gross * (1 - 1e-6)
        assert t.quantity > 0
        assert t.stop_loss < t.reference_price


def test_defensive_regime_adds_short_futures_hedge(rm, panel):
    """Force DEFENSIVE by handing the engine an index that has rolled over."""
    closes, highs, lows, volumes, index_close, symbols = panel
    falling = index_close.copy()
    falling.iloc[-30:] = falling.iloc[-30] * np.linspace(1.0, 0.80, 30)
    plan = rm.generate_plan(
        as_of=date(2023, 6, 2), closes=closes, highs=highs, lows=lows,
        volumes=volumes, index_close=falling, universe=symbols,
        capital=10_000_000.0,
    )
    assert plan.regime == MarketRegime.DEFENSIVE
    assert plan.gross_exposure == pytest.approx(rm.config.defensive_gross_exposure)
    if plan.targets:
        assert plan.hedge is not None
        assert plan.hedge.side == OrderSide.SELL
        assert plan.hedge.lots >= 1


def test_illiquid_names_are_filtered_out(rm, panel):
    closes, highs, lows, volumes, index_close, symbols = panel
    thin = volumes.copy()
    thin.iloc[:, :] = 100.0  # every name now well under Rs 50 Cr ADTV
    plan = rm.generate_plan(
        as_of=date(2023, 6, 2), closes=closes, highs=highs, lows=lows,
        volumes=thin, index_close=index_close, universe=symbols,
        capital=1_000_000.0,
    )
    assert plan.skipped
    assert "NO_CANDIDATES" in plan.skip_reason


def test_blackout_and_thin_universe_skip(rm, panel):
    closes, highs, lows, volumes, index_close, symbols = panel
    plan = rm.generate_plan(
        as_of=date(2024, 2, 1), closes=closes, highs=highs, lows=lows,
        volumes=volumes, index_close=index_close, universe=symbols,
        capital=1_000_000.0, blackout=True,
    )
    assert plan.skipped and "BLACKOUT" in plan.skip_reason

    thin = rm.generate_plan(
        as_of=date(2023, 6, 2), closes=closes, highs=highs, lows=lows,
        volumes=volumes, index_close=index_close, universe=symbols[:20],
        capital=1_000_000.0,
    )
    assert thin.skipped and "UNIVERSE_TOO_THIN" in thin.skip_reason


def test_monitor_fires_atr_stop_and_ema_trail(rm, panel):
    closes, _, _, _, _, _ = panel
    sym = "STK00"
    last = float(closes[sym].iloc[-1])

    stopped = rm.monitor(
        session_date=date(2023, 6, 2),
        holdings={sym: {"quantity": 10, "entry_price": last, "stop_loss": last * 0.95}},
        closes=closes,
        intraday_lows={sym: last * 0.94},
    )
    assert stopped and stopped[0].reason == "ATR_VOLATILITY_STOP"

    falling = closes.copy()
    falling.iloc[-5:, falling.columns.get_loc(sym)] *= 0.90
    trailed = rm.monitor(
        session_date=date(2023, 6, 2),
        holdings={sym: {"quantity": 10, "entry_price": last * 0.5, "stop_loss": 1.0}},
        closes=falling,
    )
    assert trailed and trailed[0].reason == "EMA20_TRAILING_STOP"


# --------------------------------------------------------------------- #
# NSE-VRP-INDEX
# --------------------------------------------------------------------- #
def _chain(spot=22000.0, step=50, width=40, iv=0.14):
    rows = []
    for k in range(int(spot) - width * step, int(spot) + width * step + 1, step):
        for ot in ("CE", "PE"):
            moneyness = abs(k - spot) / spot
            price = max(spot * 0.02 * np.exp(-40 * moneyness), 0.5)
            rows.append({
                "tradingsymbol": f"NIFTY24JUN{k}{ot}",
                "strike": float(k), "option_type": ot, "iv": iv,
                "last_price": round(price, 2),
                "bid": round(price * 0.99, 2), "ask": round(price * 1.01, 2),
            })
    return pd.DataFrame(rows)


def _vol_series(n=120, rv=11.0, vix=16.0, rich=True):
    """Realistic-ish series: the spread has to actually vary or the 60-day
    z-score is undefined. `rich` pushes the last print above the baseline."""
    idx = pd.bdate_range("2024-01-01", periods=n)
    rng = np.random.default_rng(11)
    rv_s = pd.Series(rv + rng.normal(0, 0.5, n), index=idx)
    vix_s = pd.Series(vix + rng.normal(0, 0.5, n), index=idx)
    if rich:
        vix_s.iloc[-1] = vix + 2.0
    return rv_s, vix_s


def test_parkinson_volatility_is_positive_and_scaled():
    bars = pd.DataFrame({"high": [100.5] * 75, "low": [99.5] * 75})
    pv = parkinson_volatility(bars)
    assert 0 < pv < 200
    flat = parkinson_volatility(pd.DataFrame({"high": [100.0] * 75, "low": [100.0] * 75}))
    assert flat == 0.0


def test_bs_delta_signs_and_bounds():
    assert 0 < bs_delta(22000, 22500, 7 / 365, 0.14, "CE") < 1
    assert -1 < bs_delta(22000, 21500, 7 / 365, 0.14, "PE") < 0
    assert bs_delta(22000, 22000, 0, 0.14, "CE") == 0.0


def test_iron_condor_is_defined_risk_and_delta_balanced():
    strat = VRPHarvestStrategy(VRPConfig())
    rv, vix = _vol_series()
    order = strat.generate_plan(
        as_of=date(2024, 6, 6), spot=22000.0, option_chain=_chain(),
        expiry=date(2024, 6, 13), rv_series=rv, india_vix=vix,
    )
    assert order is not None
    assert len(order.legs) == 4
    assert order.net_credit > 0
    assert order.max_loss > 0 and np.isfinite(order.max_loss)

    by = {f"{l.side.value}_{l.option_type}": l for l in order.legs}
    assert by["BUY_CE"].strike > by["SELL_CE"].strike
    assert by["BUY_PE"].strike < by["SELL_PE"].strike
    net_delta = sum(l.delta * (1 if l.side == OrderSide.BUY else -1) for l in order.legs)
    assert abs(net_delta) < 0.15  # roughly delta neutral at entry


def test_vix_ceiling_disengages_option_selling():
    strat = VRPHarvestStrategy(VRPConfig())
    rv, vix = _vol_series(rv=11.0, vix=16.0)
    vix.iloc[-1] = 31.0  # jump-risk regime
    assert strat.generate_plan(
        as_of=date(2024, 6, 6), spot=22000.0, option_chain=_chain(),
        expiry=date(2024, 6, 13), rv_series=rv, india_vix=vix,
    ) is None


def test_low_vrp_zscore_blocks_entry():
    strat = VRPHarvestStrategy(VRPConfig())
    idx = pd.bdate_range("2024-01-01", periods=120)
    rng = np.random.default_rng(3)
    rv = pd.Series(11.0 + rng.normal(0, 0.2, 120), index=idx)
    vix = pd.Series(16.0 + rng.normal(0, 0.2, 120), index=idx)
    vix.iloc[-1] = 13.0  # spread collapses -> z goes sharply negative
    assert strat.generate_plan(
        as_of=date(2024, 6, 6), spot=22000.0, option_chain=_chain(),
        expiry=date(2024, 6, 13), rv_series=rv, india_vix=vix,
    ) is None


def test_structure_exits_on_target_stop_and_expiry():
    strat = VRPHarvestStrategy(VRPConfig())
    rv, vix = _vol_series()
    order = strat.generate_plan(
        as_of=date(2024, 6, 6), spot=22000.0, option_chain=_chain(),
        expiry=date(2024, 6, 13), rv_series=rv, india_vix=vix,
    )
    now = datetime(2024, 6, 10, 12, 0)

    decayed = {l.tradingsymbol: l.premium * 0.2 for l in order.legs}
    sig = strat.monitor(order, decayed, now)
    assert sig and sig[0].reason == "PROFIT_TARGET_65PCT_DECAY"
    assert len(sig) == 4  # all four legs close together, never partially

    blown = {}
    for l in order.legs:
        blown[l.tradingsymbol] = l.premium * (8.0 if l.side == OrderSide.SELL else 1.0)
    sig = strat.monitor(order, blown, now)
    assert sig and sig[0].reason == "STRUCTURE_STOP_LOSS"

    flat = {l.tradingsymbol: l.premium for l in order.legs}
    sig = strat.monitor(order, flat, datetime(2024, 6, 13, 14, 35))
    assert sig and sig[0].reason == "EXPIRY_TIME_STOP"


# --------------------------------------------------------------------- #
# Costs
# --------------------------------------------------------------------- #
def test_delivery_stt_is_charged_on_both_legs():
    line = DeliveryCostCalculator().calculate(buy_price=1000.0, sell_price=1000.0, quantity=100)
    assert line.stt == pytest.approx(200.0, rel=1e-6)  # 0.10% x 2 on Rs 1,00,000
    assert line.brokerage == 0.0
    assert line.total_cost > line.stt


def test_option_stt_is_on_premium_not_notional():
    calc = IndexOptionsCostCalculator()
    line = calc.calculate_leg(entry_premium=100.0, exit_premium=35.0, quantity=75, is_short=True)
    assert line.stt == pytest.approx(100.0 * 75 * 0.0010, rel=1e-6)
    assert line.total_cost < 100.0 * 75  # sanity: costs below the premium itself


def test_rm100_backtester_runs_end_to_end(panel):
    from backtest.rm100_backtest import RM100Backtester

    closes, highs, lows, volumes, index_close, symbols = panel
    bt = RM100Backtester(ResidualMomentumStrategy(), initial_capital=2_000_000.0)
    res = bt.run(
        closes=closes, highs=highs, lows=lows, volumes=volumes,
        index_close=index_close, static_universe=symbols,
    )
    assert len(res.equity_curve) > 0
    assert res.rebalances >= 1
    assert res.total_costs > 0
    assert "sharpe" in res.stats
