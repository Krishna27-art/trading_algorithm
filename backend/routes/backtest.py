"""
Offline research / backtest routes — ISOLATED from the live signal path.

Split out of backend/signals.py. Nothing here is imported by the live stream
pipeline, every route requires X-Shared-Secret, and the whole router can be
disabled with ENABLE_BACKTEST_ROUTES=0.

Fixes vs. the previous implementation (which was duplicated in two places):
  * Unresolved instrument token is an error — it is never replaced by `0`.
  * Unknown `strategy` is a 400 — it no longer silently runs ORB while the
    response claimed the requested strategy name.
  * The shared settings.instruments[0] object is deep-copied before mutation
    (it used to be mutated per-request: cross-request race on global config).
  * One backtest at a time (429 otherwise) so offline research cannot starve
    the live signal path of CPU.
"""

import copy
import logging
import math
import threading
from datetime import timedelta
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, status

from backend.security import verify_shared_secret
from backend.broker.kite_adapter import get_active_kite_with_diagnostics
from backend.data.time_utils import today_ist

logger = logging.getLogger("backend_api.backtest")

router = APIRouter(dependencies=[Depends(verify_shared_secret)])

_backtest_slot = threading.Semaphore(1)

# strategy key -> (display name, accepted aliases)
STRATEGIES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "gap_fade": ("Gap Fade (fade a 1%+ opening gap after 30 min)", ("gap_fade", "gap")),
    "rsi_ema": ("RSI x 9 EMA (VWAP + volume filtered)", ("rsi_ema", "rsi")),
    # Did not pass its own backtest (lost money in the first half): experimental.
    "orb_sip": ("ORB 30-min, stocks in play (experimental)", ("orb_sip", "orb")),
}

# Strategies compared side by side in research
RESEARCH_STRATEGIES = ["gap_fade", "rsi_ema", "orb_sip"]

DEFAULT_SYMBOL = "RELIANCE"


def _canonical_strategy(name: str) -> Optional[str]:
    n = (name or "").lower()
    for key, (_, aliases) in STRATEGIES.items():
        if n == key or n in aliases:
            return key
    return None


def _safe_pf(pf: Optional[float]) -> Tuple[Optional[float], str]:
    if pf is None:
        return None, "UNDEFINED"
    if pf == float("inf"):
        return None, "INFINITE_NO_LOSSES"
    if math.isnan(pf) or pf != pf:
        return None, "UNDEFINED"
    return round(float(pf), 2), "CALCULATED"


class StrategyUnavailable(Exception):
    """A strategy cannot be backtested on this data; reported, never faked."""

    def __init__(self, state: str, reason: str):
        super().__init__(reason)
        self.state = state
        self.reason = reason


def _make_factory(key: str, inst, kite=None, ctx: Any = None) -> Callable[[], Any]:
    from backend.strategy import STRATEGIES as STRATEGY_CLASSES

    cls = STRATEGY_CLASSES.get(key)
    if cls is None:
        raise HTTPException(status_code=400, detail=f"Unknown strategy '{key}'.")
    return lambda: cls(inst.symbol)


def _resolve_instrument(symbol: str, kite, instrument_type: Optional[str] = None):
    from backend.config.settings import InstrumentType, settings
    from backend.config.universe import create_instrument_config_for_equity, resolve_universe_tokens
    from backend.data.instrument_resolver import instrument_resolver

    clean_sym = (symbol or DEFAULT_SYMBOL).strip().upper()
    if clean_sym not in ("NIFTY", "NIFTY 50", "BANKNIFTY"):
        token = instrument_resolver.resolve_token(clean_sym, exchange="NSE", kite_client=kite)
        if not token:
            token = resolve_universe_tokens().get(clean_sym)
        inst = create_instrument_config_for_equity(clean_sym, token)
        return inst, token or None

    if instrument_type == "FUTURES":
        fut = instrument_resolver.find_nearest_single_stock_future(clean_sym, kite_client=kite)
        if fut is not None:
            lot = instrument_resolver.resolve_lot_size(clean_sym, exchange="NFO", instrument_type="FUT", kite_client=kite)
            if lot is None or lot <= 0:
                lot = fut.get("lot_size")
            if lot is None or lot <= 0:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Unable to resolve verified futures lot size for {clean_sym} from Kite instrument master.",
                )
            inst = copy.deepcopy(settings.instruments[0])
            inst.symbol = fut["tradingsymbol"]
            inst.exchange = "NFO"
            inst.instrument_type = InstrumentType.FUTURES
            inst.lot_size = int(lot)
            inst.instrument_token = fut["instrument_token"]
            return inst, fut["instrument_token"]

    inst = copy.deepcopy(settings.instruments[0])
    inst.symbol = clean_sym
    inst.exchange = "NSE"
    inst.instrument_type = InstrumentType.INDEX
    inst.lot_size = 1
    token = instrument_resolver.resolve_token(clean_sym, exchange="NSE", kite_client=kite)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{clean_sym} instrument token unavailable from Kite.",
        )
    inst.instrument_token = token
    return inst, token


def _load_history(inst, token, days: int, kite, auth_err: Optional[str]):
    """Real Kite 15m history (cached real data allowed). Raises 400 when unavailable."""
    from backend.config.settings import settings
    from backend.data.historical_loader import HistoricalDataLoader

    cache_path = settings.base_dir / "backend" / "data" / "cache" / f"{inst.symbol}_15m_{days}d.csv"
    df = None
    fetch_error: Optional[str] = None

    if cache_path.exists():
        try:
            df, meta = HistoricalDataLoader.load_cached_data_with_validation(cache_path)
            if meta is not None:
                cached_sym = str(meta.get("symbol", "")).strip().upper()
                cached_tok = meta.get("instrument_token")
                if cached_sym and cached_sym != str(inst.symbol).strip().upper():
                    df = None
                elif token and cached_tok and int(cached_tok) != int(token):
                    df = None
        except Exception as e:
            logger.info("Cached data invalid or unreadable for %s: %s", inst.symbol, e)
            df = None

    if df is None:
        if not kite:
            fetch_error = auth_err or "Zerodha Kite Connect session is not active. Please authenticate via Kite login."
        elif not token:
            fetch_error = f"Unable to resolve numerical instrument_token for {inst.symbol} from Kite instrument master."
        else:
            try:
                today = today_ist()
                df = HistoricalDataLoader.fetch_real_data(
                    kite_client=kite,
                    instrument_token=token,
                    start_date=today - timedelta(days=int(days * 1.5)),
                    end_date=today,
                    interval="15minute",
                    cache_path=cache_path,
                )
            except Exception as e:
                fetch_error = str(e)
                logger.error(
                    "component=backtest action=fetch_history symbol=%s token=%s error=%s",
                    inst.symbol, token, e,
                )
                df = None

    if df is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Historical data unavailable for {inst.symbol} (Token: {token}): "
                f"{fetch_error or 'Kite returned zero candles.'}"
            ),
        )
    return df


def _load_backtest_data(inst, token, days: int, kite, auth_err: Optional[str]):
    """Target history with irregular NSE sessions (DR drills, Muhurat) removed."""
    from backend.data.historical_loader import exclude_irregular_sessions

    df = _load_history(inst, token, days, kite, auth_err)
    df, dropped = exclude_irregular_sessions(df)
    if dropped:
        logger.info(
            "component=backtest symbol=%s excluded_irregular_sessions=%s",
            inst.symbol, [str(d) for d in dropped],
        )
    if df is None or df.empty:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No regular-session candles available for {inst.symbol}.",
        )
    return df, [str(d) for d in dropped]


def _make_backtester(key: str, inst, kite=None, ctx: Any = None):
    from backend.backtest.strategy_backtester import StrategyBacktester
    from backend.config.settings import settings

    return StrategyBacktester(
        strategy_factory=_make_factory(key, inst),
        instrument=inst,
        app_settings=settings,
        persist_trades=False,
    )


def _report_for(trades, df, initial_capital: float):
    import pandas as pd
    from backend.backtest.performance import PerformanceAnalyzer

    dts = pd.to_datetime(df["datetime"])
    return PerformanceAnalyzer.generate_report(
        trades,
        initial_capital=initial_capital,
        backtest_start_date=dts.min(),
        backtest_end_date=dts.max(),
        all_trading_dates=sorted(dts.dt.date.unique().tolist()),
    )


def _run_one_with_trades(key: str, inst, kite, df, days: int = 180, auth_err: Optional[str] = None):
    """(report, trades, skipped_entries) for one strategy."""
    from backend.config.settings import settings

    backtester = _make_backtester(key, inst)
    trades = backtester.generate_trades(df, initial_capital=settings.initial_capital)
    report = _report_for(trades, df, settings.initial_capital)
    return report, trades, list(getattr(backtester, "skipped_entries", []) or [])


def _run_one(key: str, inst, kite, df, days: int = 180, auth_err: Optional[str] = None):
    return _run_one_with_trades(key, inst, kite, df, days=days, auth_err=auth_err)[0]


def _monte_carlo(trades) -> Dict[str, Any]:
    from backend.backtest.monte_carlo import run_monte_carlo
    from backend.config.settings import settings

    return run_monte_carlo(trades, initial_capital=settings.initial_capital).to_dict()


def _serialize_rounded(rep) -> dict:
    pf_val, pf_status = _safe_pf(rep.profit_factor)
    return {
        "total_trades": rep.total_trades,
        "long_trades": rep.long_trades,
        "short_trades": rep.short_trades,
        "winning_trades": rep.winning_trades,
        "losing_trades": rep.losing_trades,
        "win_rate_pct": round(rep.win_rate_pct, 1),
        "gross_pnl": round(rep.gross_pnl, 2),
        "total_transaction_costs": round(rep.total_transaction_costs, 2),
        "net_pnl": round(rep.net_pnl, 2),
        "profit_factor": pf_val,
        "profit_factor_status": pf_status,
        "sharpe_ratio": round(rep.sharpe_ratio, 2),
        "cagr_pct": round(rep.cagr_pct, 1),
        "max_drawdown_pct": round(rep.max_drawdown_pct, 1),
        "expectancy_rupees": round(rep.expectancy_rupees, 2),
        "long_win_rate": round(rep.long_win_rate, 1),
        "short_win_rate": round(rep.short_win_rate, 1),
        "long_net_pnl": round(rep.long_net_pnl, 2),
        "short_net_pnl": round(rep.short_net_pnl, 2),
        "yearly_returns": rep.yearly_returns,
    }


def run_all_backtests(days: int = 180, symbol: str = DEFAULT_SYMBOL) -> Dict[str, Any]:
    """Run all 3 intraday strategies on validated real/cached data."""
    from backend.config.settings import settings

    kite, auth_err = get_active_kite_with_diagnostics(force_validate=False)
    inst, token = _resolve_instrument(symbol, kite)
    df, excluded_sessions = _load_backtest_data(inst, token, days, kite, auth_err)

    active = (settings.active_strategy or "").lower()
    reports: Dict[str, Any] = {}
    robustness: Dict[str, Any] = {}
    unavailable: Dict[str, Dict[str, str]] = {}
    for key in RESEARCH_STRATEGIES:
        try:
            rep, trades, _ = _run_one_with_trades(key, inst, kite, df, days=days, auth_err=auth_err)
            reports[key] = rep
            robustness[key] = _monte_carlo(trades)
        except StrategyUnavailable as exc:
            unavailable[key] = {"state": exc.state, "reason": exc.reason}

    comparison = []
    for key in RESEARCH_STRATEGIES:
        name, aliases = STRATEGIES[key]
        if key in unavailable:
            comparison.append({
                "strategy": name,
                "strategy_id": key,
                "trades": None,
                "win_rate": None,
                "profit_factor": None,
                "profit_factor_status": "UNDEFINED",
                "sharpe": None,
                "max_drawdown": None,
                "net_pnl": None,
                "state": unavailable[key]["state"],
                "execution_note": unavailable[key]["reason"],
            })
            continue
        rep = reports[key]
        pf_val, pf_status = _safe_pf(rep.profit_factor)
        comparison.append({
            "strategy": name,
            "strategy_id": key,
            "trades": rep.total_trades,
            "win_rate": round(rep.win_rate_pct, 1),
            "profit_factor": pf_val,
            "profit_factor_status": pf_status,
            "sharpe": round(rep.sharpe_ratio, 2),
            "max_drawdown": round(rep.max_drawdown_pct, 1),
            "net_pnl": round(rep.net_pnl, 2),
            "state": "ACTIVE" if active == key or active in aliases else "STANDBY",
            "mc_max_drawdown_p95": robustness[key]["max_drawdown_pct_p95"],
            "mc_probability_of_loss": robustness[key]["probability_of_loss_pct"],
            "mc_reliable": robustness[key]["reliable"],
            "execution_note": "EXPERIMENTAL: did not pass its own backtest" if key == "orb_sip" else None,
        })

    return {
        "days": days,
        "symbol": inst.symbol,
        "data_source": "REAL_KITE",
        "excluded_irregular_sessions": excluded_sessions,
        "fill_policy": "NEXT_BAR_OPEN",
        "strategies": {
            key: {**_serialize_rounded(rep), "monte_carlo": robustness[key]}
            for key, rep in reports.items()
        },
        "unavailable_strategies": unavailable,
        "comparison": comparison,
    }


def _guarded(fn, *args, **kwargs):
    if not _backtest_slot.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Another backtest is already running. Try again shortly.",
        )
    try:
        return fn(*args, **kwargs)
    finally:
        _backtest_slot.release()


@router.get("/api/research/backtest")
def get_research_backtest(days: int = 180, symbol: str = DEFAULT_SYMBOL):
    """Backtest all 3 intraday strategies and return a comparative summary."""
    return _guarded(run_all_backtests, days=days, symbol=symbol)


@router.post("/api/research/backtest")
def post_research_backtest(days: int = 180, symbol: str = DEFAULT_SYMBOL):
    """Backtest all 3 intraday strategies and return a comparative summary."""
    return _guarded(run_all_backtests, days=days, symbol=symbol)


@router.post("/api/strategy/backtest")
def trigger_backtest(days: int = 180, symbol: str = DEFAULT_SYMBOL, strategy: str = "gap_fade"):
    """Backtest one strategy over validated real Kite historical candles."""
    key = _canonical_strategy(strategy)
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown strategy '{strategy}'. Valid: {', '.join(STRATEGIES)}.",
        )
    return _guarded(_trigger_one, days, symbol, key)


def _trigger_one(days: int, symbol: str, key: str) -> Dict[str, Any]:
    kite, auth_err = get_active_kite_with_diagnostics(force_validate=False)
    inst, token = _resolve_instrument(symbol, kite)
    df, excluded_sessions = _load_backtest_data(inst, token, days, kite, auth_err)

    report, trades, skipped = _run_one_with_trades(key, inst, kite, df, days=days, auth_err=auth_err)

    return {
        "success": True,
        "strategy": key,
        "data_source": "REAL_KITE",
        "excluded_irregular_sessions": excluded_sessions,
        "fill_policy": "NEXT_BAR_OPEN",
        "skipped_entries": len(skipped),
        "robustness": {"monte_carlo": _monte_carlo(trades)},
        "report": {
            "symbol": inst.symbol,
            "strategy": key.upper(),
            "total_trades": report.total_trades,
            "long_trades": report.long_trades,
            "short_trades": report.short_trades,
            "winning_trades": report.winning_trades,
            "losing_trades": report.losing_trades,
            "win_rate_pct": round(report.win_rate_pct, 1),
            "gross_pnl": round(report.gross_pnl, 2),
            "total_transaction_costs": round(report.total_transaction_costs, 2),
            "net_pnl": round(report.net_pnl, 2),
            "profit_factor": _safe_pf(report.profit_factor),
            "sharpe_ratio": report.sharpe_ratio,
            "cagr_pct": report.cagr_pct,
            "max_drawdown_pct": report.max_drawdown_pct,
            "max_consecutive_losses": report.max_consecutive_losses,
            "avg_r_multiple": report.avg_r_multiple,
            "expectancy_rupees": report.expectancy_rupees,
            "long_win_rate": report.long_win_rate,
            "short_win_rate": report.short_win_rate,
            "long_net_pnl": report.long_net_pnl,
            "short_net_pnl": report.short_net_pnl,
            "yearly_returns": report.yearly_returns,
        },
    }


@router.post("/api/strategy/walk_forward")
def trigger_walk_forward(
    days: int = 365,
    symbol: str = DEFAULT_SYMBOL,
    strategy: str = "gap_fade",
    train_days: int = 60,
    test_days: int = 20,
):
    """
    Rolling walk-forward test of one strategy on real Kite history.

    Each fold trades ONLY sessions after everything it warmed up on; the
    out-of-sample folds are chained into one equity curve and stress-tested
    with Monte Carlo. Parameters are fixed (no per-fold optimisation), so
    nothing here can be tuned to the test data.
    """
    key = _canonical_strategy(strategy)
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown strategy '{strategy}'. Valid: {', '.join(STRATEGIES)}.",
        )
    if train_days < 5 or test_days < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="train_days must be >= 5 and test_days >= 1.",
        )
    return _guarded(_walk_forward_one, days, symbol, key, train_days, test_days)


def _walk_forward_one(days: int, symbol: str, key: str, train_days: int, test_days: int) -> Dict[str, Any]:
    from backend.backtest.rolling_walk_forward import RollingWalkForwardValidator
    from backend.config.settings import settings

    kite, auth_err = get_active_kite_with_diagnostics(force_validate=False)
    inst, token = _resolve_instrument(symbol, kite)
    df, excluded_sessions = _load_backtest_data(inst, token, days, kite, auth_err)

    validator = RollingWalkForwardValidator(
        instrument=inst,
        app_settings=settings,
        backtester_factory=lambda fold_inst: _make_backtester(key, fold_inst),
    )
    try:
        result = validator.validate(
            df,
            min_train_days=train_days,
            test_block_days=test_days,
            initial_capital=settings.initial_capital,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    oos = result.combined_out_of_sample_report
    stats = result.fold_statistics
    return {
        "success": True,
        "strategy": key,
        "symbol": inst.symbol,
        "data_source": "REAL_KITE",
        "fill_policy": "NEXT_BAR_OPEN",
        "excluded_irregular_sessions": excluded_sessions,
        "train_days": train_days,
        "test_days": test_days,
        "folds": [
            {
                "fold": f.fold_number,
                "train": f"{f.train_start} -> {f.train_end}",
                "test": f"{f.test_start} -> {f.test_end}",
                "trades": f.report.total_trades,
                "win_rate_pct": round(f.report.win_rate_pct, 1),
                "net_pnl": round(f.report.net_pnl, 2),
                "profit_factor": _safe_pf(f.report.profit_factor)[0],
            }
            for f in result.folds
        ],
        "fold_statistics": stats.__dict__ if stats is not None else None,
        "out_of_sample": _serialize_rounded(oos),
        "final_capital": round(result.final_capital, 2),
        "robustness": {"monte_carlo": _monte_carlo(result.all_out_of_sample_trades)},
    }
