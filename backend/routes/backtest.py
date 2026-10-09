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
    "orb": ("30-Min Volatility-Filtered ORB", ("orb",)),
    "cpr": ("Central Pivot Range (CPR) Regime", ("cpr",)),
    "dual_ema": ("Adaptive Dual-EMA Trend System", ("dual_ema",)),
    "apex": ("APEX-AIVEM Catalyst & Momentum", ("apex",)),
    "sector_impulse": ("Sector Impulse Transmission (SIT)", ("sector_impulse", "sit")),
    "ssf_l5_srm": ("SSF-L5-SRM Microprice & Residual Momentum", ("ssf_l5_srm", "ssf")),
    "aou_oss": ("Analytic Ornstein-Uhlenbeck Optimal-Stopping (AOU-OSS)", ("aou_oss",)),
    "crsd": ("Cross-Sectional Residual Shock Divergence (CRSD)", ("crsd",)),
}

# The 7 single-instrument directional strategies compared in research
RESEARCH_STRATEGIES = [
    "orb",
    "cpr",
    "dual_ema",
    "apex",
    "sector_impulse",
    "ssf_l5_srm",
    "aou_oss",
]


def _canonical_strategy(name: str) -> Optional[str]:
    n = (name or "").lower()
    for key, (_, aliases) in STRATEGIES.items():
        if n == key or n in aliases:
            return key
    return None


def _safe_pf(pf: Optional[float]) -> Optional[float]:
    if pf is None:
        return None
    if pf == float("inf") or pf != pf:
        return 99.9
    return pf


def _make_factory(key: str, inst, kite) -> Callable[[], Any]:
    from backend.config.settings import settings

    if key == "orb":
        from backend.strategy.orb_strategy import IntradayORBStrategy
        return lambda: IntradayORBStrategy(inst, settings.strategy)
    if key == "cpr":
        from backend.strategy.cpr_strategy import CPRRegimeBreakoutStrategy
        return lambda: CPRRegimeBreakoutStrategy(inst, settings.strategy)
    if key == "dual_ema":
        from backend.strategy.dual_ema_strategy import BufferedDualEMAStrategy
        return lambda: BufferedDualEMAStrategy(inst, settings.strategy)
    if key == "apex":
        from backend.strategy.apex_engine import ApexStrategy
        return lambda: ApexStrategy(inst, settings.strategy)
    if key == "sector_impulse":
        from backend.data.sector_peer_manager import SectorPeerManager
        from backend.strategy.sector_impulse_strategy import SectorImpulseStrategy
        ctx = SectorPeerManager.build_peer_context(inst.symbol, kite_client=kite)
        return lambda: SectorImpulseStrategy(inst, settings.strategy, ctx=ctx)
    if key == "ssf_l5_srm":
        # Order-book strategy: 15m OHLCV has no depth, so this only exercises its regime gate.
        from backend.strategy.ssf_l5_srm_strategy import SsfL5SrmStrategy
        return lambda: SsfL5SrmStrategy(inst, settings.strategy)
    if key == "aou_oss":
        from backend.strategy.aou_oss_strategy import AouOssStrategy
        return lambda: AouOssStrategy(inst, settings.strategy)
    if key == "crsd":
        from backend.data.sector_peer_manager import SectorPeerManager
        from backend.strategy.crsd_strategy import CRSDStrategy
        peer_ctx = SectorPeerManager.build_peer_context(inst.symbol, kite_client=kite)
        return lambda: CRSDStrategy(inst, settings.strategy, ctx=peer_ctx)
    raise HTTPException(status_code=400, detail=f"Unknown strategy '{key}'.")


def _resolve_instrument(symbol: str, kite):
    """Returns (instrument_config, token_or_None). Never fabricates a token."""
    from backend.config.settings import settings
    from backend.config.universe import create_instrument_config_for_equity, resolve_universe_tokens
    from backend.data.instrument_resolver import instrument_resolver

    if symbol and symbol != "NIFTY":
        token = instrument_resolver.resolve_token(symbol, exchange="NSE", kite_client=kite)
        if not token:
            token = resolve_universe_tokens().get(symbol)
        inst = create_instrument_config_for_equity(symbol, token)
        return inst, token or None

    inst = copy.deepcopy(settings.instruments[0])  # never mutate shared settings
    token = instrument_resolver.resolve_token("NIFTY", exchange="NSE", kite_client=kite)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NIFTY instrument token unavailable from Kite.",
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
            df, _ = HistoricalDataLoader.load_cached_data_with_validation(cache_path)
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


def _run_one(key: str, inst, kite, df):
    from backend.config.settings import settings

    if key == "crsd":
        from backend.backtest.pair_backtester import PairBacktester
        from backend.data.sector_peer_manager import SectorPeerManager
        from backend.strategy.crsd_strategy import CRSDStrategy

        peer_ctx = SectorPeerManager.build_peer_context(inst.symbol, kite_client=kite)
        backtester = PairBacktester(
            strategy_factory=lambda: CRSDStrategy(inst, settings.strategy, ctx=peer_ctx),
            instrument=inst,
            peer_context=peer_ctx,
            app_settings=settings,
        )
        return backtester.run(df, initial_capital=settings.initial_capital)

    from backend.backtest.strategy_backtester import StrategyBacktester

    backtester = StrategyBacktester(
        strategy_factory=_make_factory(key, inst, kite),
        instrument=inst,
        app_settings=settings,
        persist_trades=False,
    )
    return backtester.run(df, initial_capital=settings.initial_capital)


def _serialize_rounded(rep) -> dict:
    pf = _safe_pf(rep.profit_factor)
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
        "profit_factor": round(pf, 2) if pf is not None else 0.0,
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


def run_all_backtests(days: int = 180, symbol: str = "NIFTY") -> Dict[str, Any]:
    """Run all 7 single-instrument intraday strategies on validated real/cached data."""
    from backend.config.settings import settings

    kite, auth_err = get_active_kite_with_diagnostics(force_validate=False)
    inst, token = _resolve_instrument(symbol, kite)
    df = _load_history(inst, token, days, kite, auth_err)

    active = (settings.active_strategy or "").lower()
    reports = {key: _run_one(key, inst, kite, df) for key in RESEARCH_STRATEGIES}

    comparison = []
    for key in RESEARCH_STRATEGIES:
        rep = reports[key]
        name, aliases = STRATEGIES[key]
        pf = _safe_pf(rep.profit_factor)
        comparison.append({
            "strategy": name,
            "strategy_id": key,
            "trades": rep.total_trades,
            "win_rate": round(rep.win_rate_pct, 1),
            "profit_factor": round(pf, 2) if pf is not None else 0.0,
            "sharpe": round(rep.sharpe_ratio, 2),
            "max_drawdown": round(rep.max_drawdown_pct, 1),
            "net_pnl": round(rep.net_pnl, 2),
            "state": "ACTIVE" if active == key or active in aliases else "STANDBY",
        })

    return {
        "days": days,
        "symbol": inst.symbol,
        "data_source": "REAL_KITE",
        "strategies": {key: _serialize_rounded(rep) for key, rep in reports.items()},
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
def get_research_backtest(days: int = 180, symbol: str = "NIFTY"):
    """Backtest all 7 intraday strategies and return a comparative summary."""
    return _guarded(run_all_backtests, days=days, symbol=symbol)


@router.post("/api/research/backtest")
def post_research_backtest(days: int = 180, symbol: str = "NIFTY"):
    """Backtest all 7 intraday strategies and return a comparative summary."""
    return _guarded(run_all_backtests, days=days, symbol=symbol)


@router.post("/api/strategy/backtest")
def trigger_backtest(days: int = 180, symbol: str = "NIFTY", strategy: str = "cpr"):
    """Backtest one strategy over validated real Kite historical candles."""
    if (strategy or "").lower() == "rm100":
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="RM-100 is a research/experimental strategy and is not wired into this backend.",
        )
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
    df = _load_history(inst, token, days, kite, auth_err)

    if key == "crsd":
        from backend.backtest.pair_backtester import PairBacktester
        from backend.backtest.performance import PerformanceAnalyzer
        from backend.data.sector_peer_manager import SectorPeerManager
        from backend.strategy.crsd_strategy import CRSDStrategy
        from backend.config.settings import settings

        peer_ctx = SectorPeerManager.build_peer_context(inst.symbol, kite_client=kite)
        backtester = PairBacktester(
            strategy_factory=lambda: CRSDStrategy(inst, settings.strategy, ctx=peer_ctx),
            instrument=inst,
            peer_context=peer_ctx,
            app_settings=settings,
        )
        trades = backtester.generate_trades(df, initial_capital=settings.initial_capital)
        report = PerformanceAnalyzer.generate_report(trades, initial_capital=settings.initial_capital)

        target_gross = sum(float(t.get("target_gross_pnl", 0)) for t in trades)
        hedge_gross = sum(float(t.get("hedge_gross_pnl", 0)) for t in trades)
        target_cost = sum(float(t.get("target_cost", 0)) for t in trades)
        hedge_cost = sum(float(t.get("hedge_cost", 0)) for t in trades)
        total_costs = target_cost + hedge_cost
        peer_symbols = peer_ctx.peer_symbols if peer_ctx else []

        return {
            "success": True,
            "strategy": key,
            "data_source": "REAL_KITE",
            "pair_info": {
                "target_symbol": inst.symbol,
                "hedge_basket": peer_symbols,
                "target_gross_pnl": round(target_gross, 2),
                "hedge_gross_pnl": round(hedge_gross, 2),
                "target_costs": round(target_cost, 2),
                "hedge_costs": round(hedge_cost, 2),
                "total_leg_costs": round(total_costs, 2),
            },
            "trades": trades,
            "report": {
                "symbol": inst.symbol,
                "strategy": "CRSD",
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

    report = _run_one(key, inst, kite, df)

    return {
        "success": True,
        "strategy": key,
        "data_source": "REAL_KITE",
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
