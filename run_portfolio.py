"""
CLI for the two portfolio-level engines.

run_algo.py drives BaseStrategy instruments through intraday bars; these two
engines are EOD/portfolio-level, so they get their own entry point rather
than being bolted into build_backtester().

  python run_portfolio.py --strategy rm100 --mode plan     --data data/daily
  python run_portfolio.py --strategy rm100 --mode backtest --data data/daily
  python run_portfolio.py --strategy vrp   --mode plan     --data data/vrp

Expected files under --data for rm100 (wide CSVs, first column = date):
  close.csv  high.csv  low.csv  volume.csv   (columns = NSE tradingsymbols)
  nifty50.csv                                (date,close)
  universe.csv   optional: date,symbol -- point-in-time NIFTY 100 membership.
                 Omit it and the run falls back to the static column set,
                 which carries survivorship bias; you will be warned.

For vrp:
  rv.csv        date,parkinson_rv      (or intraday_5min.csv to compute it)
  india_vix.csv date,close
  chain.csv     tradingsymbol,strike,option_type,iv,last_price[,bid,ask]
  spot.csv      date,close
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from strategy.residual_momentum import ResidualMomentumConfig, ResidualMomentumStrategy
from strategy.vrp_index import VRPConfig, VRPHarvestStrategy


def _wide(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=[0], index_col=0)
    return df.sort_index()


def _series(path: Path, column: str = "close") -> pd.Series:
    df = pd.read_csv(path, parse_dates=[0], index_col=0).sort_index()
    return df[column]


def _universe_by_date(path: Path) -> Dict[date, List[str]]:
    df = pd.read_csv(path, parse_dates=["date"])
    return {d.date(): g["symbol"].tolist() for d, g in df.groupby("date")}


def run_rm100(data_dir: Path, mode: str, capital: float, rf: float) -> None:
    closes = _wide(data_dir / "close.csv")
    highs = _wide(data_dir / "high.csv")
    lows = _wide(data_dir / "low.csv")
    volumes = _wide(data_dir / "volume.csv")
    index_close = _series(data_dir / "nifty50.csv")

    upath = data_dir / "universe.csv"
    universe_by_date = _universe_by_date(upath) if upath.exists() else None
    static_universe = list(closes.columns)

    strategy = ResidualMomentumStrategy(ResidualMomentumConfig())

    if mode == "backtest":
        from backtest.rm100_backtest import RM100Backtester

        result = RM100Backtester(strategy, initial_capital=capital).run(
            closes=closes, highs=highs, lows=lows, volumes=volumes,
            index_close=index_close, universe_by_date=universe_by_date,
            static_universe=None if universe_by_date else static_universe,
            rf_yield_pct=rf,
        )
        print("\n" + "=" * 70)
        print("  NSE-RM-100 BACKTEST")
        print("=" * 70)
        print(f"  Sessions        : {len(result.equity_curve)}")
        print(f"  Rebalances      : {result.rebalances}")
        print(f"  Trades          : {len(result.trades)}")
        print(f"  Total costs     : Rs {result.total_costs:,.2f}")
        print(f"  Final equity    : Rs {result.equity_curve.iloc[-1]:,.2f}")
        for k, v in result.stats.items():
            print(f"  {k:<15} : {v}")
        print("\n  Hurdles from the research plan: Sharpe >= 1.30, Sortino >= 1.75,")
        print("  max DD <= 16%, Calmar >= 1.25. A single split clearing these is")
        print("  not evidence — run the 10-fold purged walk-forward before deploying.")
        return

    as_of = closes.index[-1].date()
    universe = (universe_by_date or {}).get(as_of, static_universe)
    plan = strategy.generate_plan(
        as_of=as_of, closes=closes, highs=highs, lows=lows, volumes=volumes,
        index_close=index_close, universe=universe, capital=capital, rf_yield_pct=rf,
    )
    print(f"\nNSE-RM-100 plan for {as_of} | regime={plan.regime.value} "
          f"| gross={plan.gross_exposure:.0%}")
    if plan.skipped:
        print(f"  SKIPPED: {plan.skip_reason}")
        return
    print(f"  {'SYM':<14}{'RANK':>5}{'Z':>8}{'WEIGHT':>9}{'QTY':>8}{'STOP':>10}")
    for t in plan.targets:
        print(f"  {t.symbol:<14}{t.rank:>5}{t.score:>8.2f}{t.weight:>9.2%}"
              f"{t.quantity:>8}{(t.stop_loss or 0):>10.2f}")
    print("\n  Orders:")
    for o in plan.orders:
        print(f"    {o.side.value:<5}{o.quantity:>7} {o.symbol:<14} @~{o.reference_price:>9.2f}  {o.reason}")
    if plan.hedge:
        h = plan.hedge
        print(f"\n  Hedge: SELL {h.lots} lot(s) {h.symbol} "
              f"(beta {h.portfolio_beta}, notional Rs {h.notional:,.0f})")


def run_vrp(data_dir: Path, expiry: Optional[str]) -> None:
    rv = _series(data_dir / "rv.csv", "parkinson_rv")
    vix = _series(data_dir / "india_vix.csv")
    spot = float(_series(data_dir / "spot.csv").iloc[-1])
    chain = pd.read_csv(data_dir / "chain.csv")

    strategy = VRPHarvestStrategy(VRPConfig())
    as_of = rv.index[-1].date()
    exp = datetime.strptime(expiry, "%Y-%m-%d").date() if expiry else None
    if exp is None:
        raise SystemExit("--expiry YYYY-MM-DD is required for the vrp strategy")

    sig = strategy.vrp_signal(rv, vix)
    print(f"\nNSE-VRP-INDEX {as_of} | VIX={sig['iv']:.2f} RV20={sig['rv20']:.2f} "
          f"spread={sig['spread']:.2f} z={sig['z']:.2f}")

    order = strategy.generate_plan(
        as_of=as_of, spot=spot, option_chain=chain, expiry=exp,
        rv_series=rv, india_vix=vix,
    )
    if order is None:
        print("  NO ENTRY — gates not satisfied. This is the expected output most weeks.")
        return
    print(f"  {order.structure_type} {order.structure_id}")
    for leg in order.legs:
        print(f"    {leg.side.value:<5}{leg.lots:>3} lot {leg.tradingsymbol:<22}"
              f"K={leg.strike:<9.0f} d={leg.delta:+.3f} prem={leg.premium:>8.2f}")
    print(f"  Net credit Rs {order.net_credit:,.2f} | max loss Rs {order.max_loss:,.2f}")
    print(f"  Target Rs {order.profit_target:,.2f} | stop Rs {order.stop_loss:,.2f} "
          f"| hard exit 14:30 on {order.expiry}")


def main() -> None:
    p = argparse.ArgumentParser(description="Portfolio-level strategy runner")
    p.add_argument("--strategy", choices=["rm100", "vrp"], required=True)
    p.add_argument("--mode", choices=["plan", "backtest"], default="plan")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--capital", type=float, default=1_000_000.0)
    p.add_argument("--rf", type=float, default=6.5, help="91-day T-bill yield %% p.a.")
    p.add_argument("--expiry", type=str, default=None, help="vrp: YYYY-MM-DD")
    args = p.parse_args()

    if args.strategy == "rm100":
        run_rm100(args.data, args.mode, args.capital, args.rf)
    else:
        run_vrp(args.data, args.expiry)


if __name__ == "__main__":
    main()
