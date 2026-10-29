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
    "orb": ("30-Min Volatility-Filtered ORB", ("orb",)),
    "cpr": ("Central Pivot Range (CPR) Regime", ("cpr",)),
    "dual_ema": ("Adaptive Dual-EMA Trend System", ("dual_ema",)),
    "apex": ("APEX-AIVEM Catalyst & Momentum", ("apex",)),
    "sector_impulse": ("Sector Impulse Transmission (SIT)", ("sector_impulse", "sit")),
    "ssf_l5_srm": ("SSF-L5-SRM Microprice & Residual Momentum", ("ssf_l5_srm", "ssf")),
    "aou_oss": ("Analytic Ornstein-Uhlenbeck Optimal-Stopping (AOU-OSS)", ("aou_oss",)),
    "crsd": ("Cross-Sectional Residual Shock Divergence (CRSD)", ("crsd",)),
    # Research only: not part of live consensus until proven out-of-sample.
    "orb_retest": ("ORB Retest (15m range + ATR/RVOL, research)", ("orb_retest",)),
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


def _peer_history(symbol: str, days: int, kite, auth_err: Optional[str]):
    """Real Kite 15m history for a peer over the SAME window as the target."""
    from backend.data.historical_loader import exclude_irregular_sessions

    inst, token = _resolve_instrument(symbol, kite)
    df = _load_history(inst, token, days, kite, auth_err)
    df, _ = exclude_irregular_sessions(df)
    return df


def _backtest_sector_context(inst, days: int, kite, auth_err: Optional[str]):
    """Leader / market / sector frames covering the full backtest window."""
    from backend.data.models import PeerContext
    from backend.data.sector_peer_manager import SectorPeerManager

    try:
        leader, market, sector = SectorPeerManager.get_peer_symbols(inst.symbol)
    except ValueError:
        raise StrategyUnavailable(
            "NOT_APPLICABLE", f"{inst.symbol} has no sector classification."
        )
    if sector == inst.symbol:
        # A sector leader cannot be its own sector reference.
        raise StrategyUnavailable(
            "NOT_APPLICABLE",
            f"{inst.symbol} is the sector reference itself; no independent sector leg.",
        )
    frames = {}
    for role, sym in (("leader", leader), ("market", market), ("sector", sector)):
        try:
            frames[role] = _peer_history(sym, days, kite, auth_err)
        except HTTPException as exc:
            raise StrategyUnavailable("UNAVAILABLE", f"{role} {sym}: {exc.detail}")
    return PeerContext(leader=frames["leader"], market=frames["market"], sector=frames["sector"])


def _backtest_crsd_context(inst, days: int, kite, auth_err: Optional[str]):
    """CRSD peer basket + NIFTY market frame covering the full backtest window."""
    from backend.data.sector_peer_manager import SectorPeerManager
    from backend.strategy.crsd_strategy import CRSDPeerContext

    sec = SectorPeerManager.get_sector_for_symbol(inst.symbol)
    if sec is None:
        raise StrategyUnavailable(
            "NOT_APPLICABLE", f"{inst.symbol} has no sector classification."
        )
    try:
        market = _peer_history("NIFTY", days, kite, auth_err)
    except HTTPException as exc:
        raise StrategyUnavailable("UNAVAILABLE", f"market NIFTY: {exc.detail}")

    peers = {}
    failures = []
    for sym in [m for m in sec.constituents if m != inst.symbol][:8]:
        try:
            peers[sym] = _peer_history(sym, days, kite, auth_err)
        except HTTPException as exc:
            failures.append(f"{sym}: {exc.detail}")
    if len(peers) < 2:
        raise StrategyUnavailable(
            "UNAVAILABLE",
            "CRSD needs at least 2 peers with real history; "
            + ("; ".join(failures[:3]) or "none available."),
        )
    return CRSDPeerContext(peers=peers, market=market)


def _make_factory(key: str, inst, kite, ctx: Any = None) -> Callable[[], Any]:
    from backend.config.settings import settings

    if key == "orb":
        from backend.strategy.orb_strategy import IntradayORBStrategy
        return lambda: IntradayORBStrategy(inst, settings.strategy)
    if key == "orb_retest":
        from backend.strategy.orb_retest_strategy import ORBRetestStrategy
        return lambda: ORBRetestStrategy(inst, settings.strategy)
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
        from backend.strategy.sector_impulse_strategy import SectorImpulseStrategy
        return lambda: SectorImpulseStrategy(inst, settings.strategy, ctx=ctx)
    if key == "ssf_l5_srm":
        # Order-book strategy: 15m OHLCV has no depth, so this only exercises its regime gate.
        from backend.strategy.ssf_l5_srm_strategy import SsfL5SrmStrategy
        return lambda: SsfL5SrmStrategy(inst, settings.strategy)
    if key == "aou_oss":
        from backend.strategy.aou_oss_strategy import AouOssStrategy
        return lambda: AouOssStrategy(inst, settings.strategy)
    if key == "crsd":
        from backend.strategy.crsd_strategy import CRSDStrategy
        return lambda: CRSDStrategy(inst, settings.strategy, ctx=ctx)
    raise HTTPException(status_code=400, detail=f"Unknown strategy '{key}'.")


def _resolve_instrument(symbol: str, kite, instrument_type: Optional[str] = None):
    from backend.config.settings import InstrumentType, settings
    from backend.config.universe import create_instrument_config_for_equity, resolve_universe_tokens
    from backend.data.instrument_resolver import instrument_resolver

    clean_sym = (symbol or "NIFTY").strip().upper()
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


def _strategy_context(key: str, inst, days: int, kite, auth_err: Optional[str]):
    if key == "sector_impulse":
        return _backtest_sector_context(inst, days, kite, auth_err)
    if key == "crsd":
        return _backtest_crsd_context(inst, days, kite, auth_err)
    return None


def _make_backtester(key: str, inst, kite, ctx: Any):
    """Backtester for one strategy (pair backtester for CRSD)."""
    from backend.config.settings import settings

    if key == "crsd":
        from backend.backtest.pair_backtester import PairBacktester
        from backend.strategy.crsd_strategy import CRSDStrategy

        return PairBacktester(
            strategy_factory=lambda: CRSDStrategy(inst, settings.strategy, ctx=ctx),
            instrument=inst,
            peer_context=ctx,
            app_settings=settings,
        )

    from backend.backtest.strategy_backtester import StrategyBacktester

    return StrategyBacktester(
        strategy_factory=_make_factory(key, inst, kite, ctx),
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

    ctx = _strategy_context(key, inst, days, kite, auth_err)
    backtester = _make_backtester(key, inst, kite, ctx)
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


def run_all_backtests(days: int = 180, symbol: str = "NIFTY") -> Dict[str, Any]:
    """Run all 7 single-instrument intraday strategies on validated real/cached data."""
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
            "execution_note": "REGIME_ONLY: Order book depth absent" if key == "ssf_l5_srm" else None,
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
    df, excluded_sessions = _load_backtest_data(inst, token, days, kite, auth_err)

    def _unavailable(exc: StrategyUnavailable):
        code = (
            status.HTTP_422_UNPROCESSABLE_ENTITY
            if exc.state == "NOT_APPLICABLE"
            else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        return HTTPException(
            status_code=code,
            detail=f"{STRATEGIES[key][0]} {exc.state.replace('_', ' ').lower()} for {inst.symbol}: {exc.reason}",
        )

    if key == "crsd":
        from backend.backtest.pair_backtester import PairBacktester
        from backend.backtest.performance import PerformanceAnalyzer
        from backend.strategy.crsd_strategy import CRSDStrategy
        from backend.config.settings import settings

        try:
            peer_ctx = _backtest_crsd_context(inst, days, kite, auth_err)
        except StrategyUnavailable as exc:
            raise _unavailable(exc)
        backtester = PairBacktester(
            strategy_factory=lambda: CRSDStrategy(inst, settings.strategy, ctx=peer_ctx),
            instrument=inst,
            peer_context=peer_ctx,
            app_settings=settings,
        )
        trades = backtester.generate_trades(df, initial_capital=settings.initial_capital)
        report = _report_for(trades, df, settings.initial_capital)

        target_gross = sum(float(t.get("target_gross_pnl", 0)) for t in trades)
        hedge_gross = sum(float(t.get("hedge_gross_pnl", 0)) for t in trades)
        target_cost = sum(float(t.get("target_cost", 0)) for t in trades)
        hedge_cost = sum(float(t.get("hedge_cost", 0)) for t in trades)
        total_costs = target_cost + hedge_cost
        peer_symbols = peer_ctx.symbols() if peer_ctx else []

        return {
            "success": True,
            "strategy": key,
            "data_source": "REAL_KITE",
            "excluded_irregular_sessions": excluded_sessions,
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
            "robustness": {"monte_carlo": _monte_carlo(trades)},
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

    try:
        report, trades, skipped = _run_one_with_trades(key, inst, kite, df, days=days, auth_err=auth_err)
    except StrategyUnavailable as exc:
        raise _unavailable(exc)

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
    symbol: str = "NIFTY",
    strategy: str = "orb",
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

    try:
        ctx = _strategy_context(key, inst, days, kite, auth_err)
    except StrategyUnavailable as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
                if exc.state == "NOT_APPLICABLE"
                else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail=f"{STRATEGIES[key][0]}: {exc.reason}",
        )

    validator = RollingWalkForwardValidator(
        instrument=inst,
        app_settings=settings,
        backtester_factory=lambda fold_inst: _make_backtester(key, fold_inst, kite, ctx),
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
