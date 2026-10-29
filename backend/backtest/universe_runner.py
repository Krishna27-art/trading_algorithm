"""
Backtest all three strategies across many stocks with real Kite data.

Run (from the project root, after logging in to Kite once through the app):

    python -m backend.backtest.universe_runner --days 365 --group large
    python -m backend.backtest.universe_runner --days 365 --group all --strategies gap_fade

Groups: large (100 large caps), mid (100 mid caps), small (500 small caps),
all (700). Large caps are the default: intraday shorting and fills are
realistic there.

What it does
- downloads 15-minute candles for each stock (cached under backend/data/cache)
- runs each strategy through the same StrategyBacktester used by the API
  (next-candle-open fills, Zerodha intraday charges + slippage)
- splits the period into two halves and reports both, so a lucky result in
  one stretch of time is easy to spot
- writes one row per stock/strategy to backend/data/cache/universe_backtest_<date>.csv
  and prints a per-strategy summary

Signal research only; nothing here places orders.
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import timedelta
from typing import Dict, List

import pandas as pd

from backend.backtest.strategy_backtester import StrategyBacktester
from backend.broker.kite_adapter import get_active_kite_with_diagnostics
from backend.config.settings import settings
from backend.config.universe import StockUniverse, create_instrument_config_for_equity, resolve_universe_tokens
from backend.data.historical_loader import HistoricalDataLoader, exclude_irregular_sessions
from backend.data.time_utils import today_ist
from backend.strategy import STRATEGIES

CAPITAL_PER_TRADE = 100_000.0  # each trade is sized as a fixed ₹1 lakh position for % returns


def _symbols(group: str) -> List[str]:
    u = StockUniverse()
    groups = {
        "large": u.large_cap_stocks,
        "mid": u.mid_cap_stocks,
        "small": u.small_cap_stocks,
        "all": u.all_stocks,
    }
    if group not in groups:
        raise SystemExit(f"Unknown group '{group}'. Use one of: {', '.join(groups)}")
    return [r.symbol for r in groups[group]]


def _load(symbol: str, token: int, days: int, kite) -> pd.DataFrame:
    cache = settings.base_dir / "backend" / "data" / "cache" / f"{symbol}_15m_{days}d.csv"
    today = today_ist()
    df = HistoricalDataLoader.fetch_real_data(
        kite_client=kite,
        instrument_token=int(token),
        start_date=today - timedelta(days=int(days * 1.5)),
        end_date=today - timedelta(days=1),
        interval="15minute",
        cache_path=cache,
    )
    df, _ = exclude_irregular_sessions(df)
    return df


def _stats(trades: List[dict]) -> Dict[str, float]:
    if not trades:
        return {"trades": 0, "win_pct": None, "avg_net_pct": None, "profit_factor": None, "net_pnl": 0.0}
    rets = [t["pnl_net"] / (t["entry_price"] * t["quantity"]) * 100.0 for t in trades]
    wins = sum(r for r in rets if r > 0)
    losses = -sum(r for r in rets if r < 0)
    return {
        "trades": len(rets),
        "win_pct": round(sum(1 for r in rets if r > 0) / len(rets) * 100.0, 1),
        "avg_net_pct": round(sum(rets) / len(rets), 3),
        "profit_factor": round(wins / losses, 2) if losses > 0 else None,
        "net_pnl": round(sum(t["pnl_net"] for t in trades), 2),
    }


def run(days: int, group: str, keys: List[str]) -> pd.DataFrame:
    kite, err = get_active_kite_with_diagnostics(force_validate=False)
    if kite is None:
        raise SystemExit(f"Kite session is not active: {err}. Log in through the app first.")

    tokens = resolve_universe_tokens(kite_client=kite)
    symbols = _symbols(group)
    rows = []
    for n, sym in enumerate(symbols, 1):
        token = tokens.get(sym)
        if not token:
            print(f"[{n}/{len(symbols)}] {sym}: no instrument token, skipped")
            continue
        try:
            df = _load(sym, token, days, kite)
        except Exception as exc:
            print(f"[{n}/{len(symbols)}] {sym}: history failed ({exc}), skipped")
            continue
        if df is None or df.empty:
            continue
        dates = sorted(pd.to_datetime(df["datetime"]).dt.date.unique())
        cut = dates[len(dates) // 2]
        inst = create_instrument_config_for_equity(sym, token)
        for key in keys:
            cls = STRATEGIES[key]
            bt = StrategyBacktester(lambda: cls(sym), inst, settings, persist_trades=False)
            bt.position_sizer.calculate_order_quantity = (  # fixed-size positions
                lambda capital=0, stop_distance=0, instrument=None, estimated_price=None, **_: 1
            )
            trades = bt.generate_trades(df, initial_capital=settings.initial_capital)
            # re-cost each trade at a fixed ₹1 lakh notional so % returns are comparable
            sized = []
            for t in trades:
                qty = max(1, int(CAPITAL_PER_TRADE // t["entry_price"]))
                scale = qty / max(1, t["quantity"])
                t = dict(t)
                t["quantity"] = qty
                gross = t["pnl_gross"] * scale
                costs = bt.cost_calculator.calculate_trade_costs(
                    sym, inst.instrument_type,
                    t["entry_price"] if t["direction"] == "BUY" else t["exit_price"],
                    t["exit_price"] if t["direction"] == "BUY" else t["entry_price"],
                    qty,
                ).total_cost
                t["pnl_net"] = gross - costs
                sized.append(t)
            first = [t for t in sized if pd.Timestamp(t["entry_time"]).date() < cut]
            second = [t for t in sized if pd.Timestamp(t["entry_time"]).date() >= cut]
            all_s, a, b = _stats(sized), _stats(first), _stats(second)
            rows.append({
                "symbol": sym, "strategy": key, **all_s,
                "first_half_avg_pct": a["avg_net_pct"], "second_half_avg_pct": b["avg_net_pct"],
                "first_half_trades": a["trades"], "second_half_trades": b["trades"],
            })
        print(f"[{n}/{len(symbols)}] {sym}: done")
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    out = []
    for key, g in df.groupby("strategy"):
        g = g[g["trades"] > 0]
        if g.empty:
            continue
        n = g["trades"].sum()

        def wavg(col):
            sub = g.dropna(subset=[col])
            w = sub["trades"] if col == "avg_net_pct" else sub[col.replace("avg_pct", "trades")]
            return round(float((sub[col] * w).sum() / w.sum()), 3) if w.sum() > 0 else math.nan

        out.append({
            "strategy": key,
            "stocks_with_trades": len(g),
            "trades": int(n),
            "avg_net_pct_per_trade": wavg("avg_net_pct"),
            "first_half_avg_pct": wavg("first_half_avg_pct"),
            "second_half_avg_pct": wavg("second_half_avg_pct"),
            "net_pnl_rupees_at_1L_per_trade": round(float(g["net_pnl"].sum()), 0),
            "profitable_stocks_pct": round(float((g["net_pnl"] > 0).mean() * 100), 1),
        })
    return pd.DataFrame(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backtest the 3 strategies across the stock universe.")
    ap.add_argument("--days", type=int, default=365, help="calendar days of history (default 365)")
    ap.add_argument("--group", default="large", help="large | mid | small | all (default large)")
    ap.add_argument("--strategies", default="all", help="comma list: gap_fade,rsi_ema,orb_sip (default all)")
    args = ap.parse_args(argv)

    keys = list(STRATEGIES) if args.strategies == "all" else [k.strip() for k in args.strategies.split(",")]
    bad = [k for k in keys if k not in STRATEGIES]
    if bad:
        raise SystemExit(f"Unknown strategies: {bad}. Use: {', '.join(STRATEGIES)}")

    df = run(args.days, args.group, keys)
    if df.empty:
        print("No results.")
        return 1
    path = settings.base_dir / "backend" / "data" / "cache" / f"universe_backtest_{today_ist()}.csv"
    df.to_csv(path, index=False)
    print()
    print(summarize(df).to_string(index=False))
    print(f"\nPer-stock results: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
