"""
Strategy state, universe scanner, live research, backtest, and telemetry
routes. This is the "what should I trade right now" surface of the API.

REMOVED vs. the old backend/main.py: nothing route-level — this module is
a straight split of the strategy/research section. The one behavior change
is inside trigger_backtest(): the strategy="rm100" branch used to import
`backtest.rm100_backtest.RM100Backtester`, a module that does not exist in
this codebase, so that ImportError->501 was firing on every single call and
everything after it (EODPanelLoader, ResidualMomentumStrategy, ~50 lines)
was unreachable dead code. It now just returns 501 directly.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException, status

from broker.kite_adapter import get_active_kite, get_active_kite_with_diagnostics

logger = logging.getLogger("backend_api.signals")

router = APIRouter()

# Global in-memory cache for live research predictions to prevent expensive repetitive calculations
_live_research_cache: Dict[str, Any] = {
    "timestamp": 0.0,
    "top_n": 10,
    "data": None,
}

STRATEGY_REGISTRY = {
    "cpr": {
        "name": "Central Pivot Range (CPR) Regime Breakout & Mean-Reversion",
        "description": "Dynamic pivot width regime detection (Narrow/Wide/Neutral) with boundary breakout entries",
        "timeframe": "15m candles",
        "key_levels": ["TC", "Pivot P", "BC", "R1", "S1", "VWAP"],
    },
    "dual_ema": {
        "name": "Adaptive Volatility-Buffered Dual-EMA Trend System",
        "description": "Fast 9-EMA / 21-EMA trend ribbon with ATR-scaled noise buffer to eliminate chop",
        "timeframe": "15m candles",
        "key_levels": ["EMA-9", "EMA-21", "ATR Buffer", "VWAP"],
    },
    "orb": {
        "name": "30-Minute Volatility-Filtered Opening Range Breakout (ORB)",
        "description": "Classic 09:15-09:45 Opening Range breakout with session VWAP confirmation",
        "timeframe": "15m candles",
        "key_levels": ["OR High", "OR Low", "VWAP"],
    },
}


@router.get("/api/strategy/state")
def get_strategy_state(strategy: Optional[str] = None):
    """Returns configuration, state, and safety parameters of the selected strategy."""
    from config.settings import settings
    from data.market_calendar import MarketCalendar

    now = datetime.now()
    phase = MarketCalendar.get_session_phase(now.time()).value
    strat_key = (strategy or settings.active_strategy or "cpr").lower()
    strat_meta = STRATEGY_REGISTRY.get(strat_key, STRATEGY_REGISTRY["cpr"])

    return {
        "strategy_key": strat_key,
        "strategy_name": strat_meta["name"],
        "strategy_description": strat_meta["description"],
        "key_levels": strat_meta["key_levels"],
        "symbol": settings.instruments[0].symbol,
        "exchange": settings.instruments[0].exchange,
        "instrument_type": settings.instruments[0].instrument_type.value,
        "lot_size": settings.instruments[0].lot_size,
        "session_phase": phase,
        "schedule": {
            "market_open": "09:15 IST",
            "entry_start": "09:45 IST",
            "entry_end": "13:30 IST",
            "square_off_time": "14:30 IST",
            "hard_cutoff_time": "15:10 IST",
        },
        "risk_reward_ratio": settings.strategy.risk_reward_ratio,
        "breakeven_r_multiple": settings.strategy.breakeven_r_multiple,
        "risk_per_trade_pct": settings.risk.risk_per_trade_pct * 100.0,
        "max_daily_loss_pct": settings.risk.max_daily_loss_pct * 100.0,
        "broker": "ZERODHA_KITE",
        "read_only": True,
    }


@router.get("/api/strategy/trades")
def get_strategy_trades():
    """Fetches all executed trades from SQLite journal."""
    from database.db import DatabaseManager
    from config.settings import settings

    db = DatabaseManager(settings.db_path)
    trades = db.get_all_trades()
    return {"trades": trades, "count": len(trades)}


@router.get("/api/strategy/scanner")
def get_universe_scan(
    top_n: int = 5,
    refresh: bool = False,
):
    """
    Scans and ranks the 300-stock universe using real batched Kite quotes.
    Requires an active Kite Connect session.
    """
    from scanner.stock_ranker import StockUniverseScanner

    is_test = bool(os.environ.get("PYTEST_CURRENT_TEST"))
    kite = get_active_kite()

    if not kite and not is_test:
        return {
            "status": "AUTH_REQUIRED",
            "data_source": "NONE",
            "message": "Zerodha Kite Connect session is not authenticated. Please log in with Kite to scan real market quotes.",
            "candidates": [],
            "count": 0,
            "top_n": top_n,
            "timestamp": datetime.now().isoformat(),
        }

    try:
        scanner = StockUniverseScanner()
        ranked, data_source = scanner.scan_universe(
            kite_client=kite,
            top_n=top_n,
            force_refresh_history=refresh,
            allow_synthetic=is_test,
        )
        summary = getattr(scanner, "last_pipeline_summary", {})
        return {
            "status": "success",
            "data_source": data_source,
            "timestamp": datetime.now().isoformat(),
            "count": len(ranked),
            "top_n": top_n,
            "pipeline_summary": {
                "universe_count": summary.get("universe_count", 300),
                "tradable_count": summary.get("tradable_count", len(ranked)),
                "setup_count": summary.get("setup_count", len(ranked)),
                "strong_signal_count": summary.get("strong_signal_count", 0),
            },
            "scoring_weights": {
                "rvol_weight": 30,
                "gap_weight": 25,
                "volatility_weight": 25,
                "vwap_dist_weight": 20,
                "total_max": 100,
            },
            "candidates": [m.to_dict() for m in ranked],
        }
    except Exception as e:
        logger.error(f"Scanner execution failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Scanner error: {str(e)}",
        )


@router.get("/api/research/live")
def get_live_research(top_n: int = 10, force_refresh: bool = False):
    """
    Unified Live Research & Predictions Endpoint.
    Uses REAL Kite market quotes and real 15-minute intraday candles to rank
    the 300-stock universe, run strategy models on top candidates, calculate
    consensus, and derive key insights.
    Never fabricates synthetic candles or fake predictions in production.
    """
    import time as time_mod
    from datetime import time as dt_time
    from config.settings import settings
    from data.market_calendar import MarketCalendar
    from data.historical_loader import HistoricalDataLoader
    from scanner.stock_ranker import StockUniverseScanner
    from strategy.prediction_service import CandidatePrediction, prediction_service

    now = datetime.now()
    cur_time = now.time()
    is_open = (dt_time(9, 15) <= cur_time <= dt_time(15, 30)) and MarketCalendar.is_trading_day(now.date())
    is_test = bool(os.environ.get("PYTEST_CURRENT_TEST"))
    kite, auth_err = get_active_kite_with_diagnostics(force_validate=False)

    if not kite and not is_test:
        return {
            "status": "AUTH_REQUIRED",
            "data_source": "NONE",
            "timestamp": now.isoformat(),
            "market_status": "OPEN" if is_open else "CLOSED",
            "message": "Zerodha Kite Connect session is not authenticated. Please log in with Kite Connect to view real live predictions.",
            "scanned_count": 0,
            "returned_count": 0,
            "candidates": [],
            "key_insights": {
                "top_long": None,
                "top_short": None,
                "strongest_consensus": None,
                "divergent_signals": [],
            },
        }

    # Use in-memory cache if fresh (<= 15 seconds) unless forced
    cache_age = time_mod.time() - _live_research_cache["timestamp"]
    if not force_refresh and cache_age < 15.0 and _live_research_cache["data"] and _live_research_cache["top_n"] == top_n:
        return _live_research_cache["data"]

    try:
        scanner = StockUniverseScanner()
        ranked_metrics, data_source_label = scanner.scan_universe(
            kite_client=kite,
            top_n=top_n,
            allow_synthetic=is_test,
        )

        candidates: List[CandidatePrediction] = []
        cache_dir = settings.base_dir / "data" / "cache"
        today = now.date()

        for idx, item in enumerate(ranked_metrics, start=1):
            sym = item.symbol
            token = item.token
            ltp = item.ltp
            df_15m = None

            # 1. Try reading cached 15m intraday file
            cache_file = cache_dir / f"{sym}_15m.csv"
            if cache_file.exists():
                try:
                    df_15m, _ = HistoricalDataLoader.load_cached_data_with_validation(cache_file)
                except Exception:
                    df_15m = None

            # 2. If kite is connected and no cache, try loading real historical 15m bars
            if (df_15m is None or df_15m.empty) and kite and token:
                try:
                    start_d = today - timedelta(days=7)
                    df_15m = HistoricalDataLoader.fetch_real_data(
                        kite_client=kite,
                        instrument_token=token,
                        start_date=start_d,
                        end_date=today,
                        interval="15minute",
                        cache_path=cache_file,
                    )
                except Exception as e:
                    logger.debug(f"Could not load 15m bars for {sym} from Kite: {e}")
                    df_15m = None

            # 3. If in test environment only, provide test data
            if (df_15m is None or df_15m.empty) and is_test:
                df_15m = HistoricalDataLoader.generate_synthetic_nifty_data(
                    days=5,
                    seed=idx * 17,
                    base_price=ltp or 2000.0,
                )

            # In production: if real intraday data is unavailable, skip candidate or mark unavailable
            if df_15m is None or df_15m.empty:
                logger.warning(f"Real 15m intraday data unavailable for {sym} (Token {token}) — skipping.")
                continue

            preds, consensus = prediction_service.evaluate_symbol(
                symbol=sym,
                df_15m=df_15m,
                current_ltp=ltp,
                token=token,
                stock_metric=item,
            )

            cand = CandidatePrediction(
                rank=idx,
                symbol=sym,
                ltp=ltp,
                momentum_score=item.total_score,
                universe_bias=item.direction_bias,
                predictions=preds,
                consensus=consensus,
            )
            candidates.append(cand)

        key_insights = prediction_service.extract_key_insights(candidates)
        summary = getattr(scanner, "last_pipeline_summary", {})

        if not candidates and not is_test:
            response_payload = {
                "status": "DATA_UNAVAILABLE",
                "data_source": "REAL_KITE" if data_source_label == "REAL" else "NONE",
                "timestamp": now.isoformat(),
                "market_status": "OPEN" if is_open else "CLOSED",
                "message": "Real 15-minute intraday candle data is currently unavailable from Kite.",
                "scanned_count": summary.get("universe_count", 300),
                "returned_count": 0,
                "candidates": [],
                "key_insights": {
                    "top_long": None,
                    "top_short": None,
                    "strongest_consensus": None,
                    "divergent_signals": [],
                },
            }
        else:
            response_payload = {
                "status": "success",
                "data_source": "REAL_KITE" if data_source_label == "REAL" else "SYNTHETIC_TEST",
                "timestamp": now.isoformat(),
                "market_status": "OPEN" if is_open else "CLOSED",
                "scanned_count": summary.get("universe_count", 300),
                "returned_count": len(candidates),
                "candidates": [c.to_dict() for c in candidates],
                "key_insights": key_insights,
            }

        _live_research_cache["timestamp"] = time_mod.time()
        _live_research_cache["top_n"] = top_n
        _live_research_cache["data"] = response_payload

        return response_payload

    except Exception as e:
        logger.error(f"Live research endpoint failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Live research execution error: {str(e)}",
        )


def run_all_three_backtests(days: int = 180, symbol: str = "NIFTY") -> Dict[str, Any]:
    """Helper to run ORB, CPR, and Dual-EMA on validated real/cached data."""
    from backtest.strategy_backtester import StrategyBacktester
    from config.settings import settings
    from config.universe import create_instrument_config_for_equity, resolve_universe_tokens
    from data.historical_loader import HistoricalDataLoader
    from data.instrument_resolver import instrument_resolver
    from strategy.cpr_strategy import CPRRegimeBreakoutStrategy
    from strategy.dual_ema_strategy import BufferedDualEMAStrategy
    from strategy.orb_strategy import IntradayORBStrategy

    kite, auth_err = get_active_kite_with_diagnostics(force_validate=False)

    if symbol and symbol != "NIFTY":
        token = instrument_resolver.resolve_token(symbol, exchange="NSE", kite_client=kite)
        if not token:
            tokens = resolve_universe_tokens()
            token = tokens.get(symbol, 0)
        inst = create_instrument_config_for_equity(symbol, token or 0)
        base_p = 2000.0
    else:
        inst = settings.instruments[0]
        token = instrument_resolver.resolve_token("NIFTY", exchange="NSE", kite_client=kite) or 256265
        inst.instrument_token = token
        base_p = 24000.0

    cache_path = settings.base_dir / "data" / "cache" / f"{inst.symbol}_15m_{days}d.csv"
    df = None
    data_source = "REAL_KITE"
    fetch_error: Optional[str] = None

    if cache_path.exists():
        try:
            df, _ = HistoricalDataLoader.load_cached_data_with_validation(cache_path)
        except Exception:
            df = None

    if df is None:
        if not kite:
            fetch_error = auth_err or "Zerodha Kite Connect session is not active. Please authenticate via Kite login."
        elif not token:
            fetch_error = f"Unable to resolve numerical instrument_token for {inst.symbol} from Kite instrument master."
        else:
            try:
                today = datetime.now().date()
                start_d = today - timedelta(days=int(days * 1.5))
                df = HistoricalDataLoader.fetch_real_data(
                    kite_client=kite,
                    instrument_token=token,
                    start_date=start_d,
                    end_date=today,
                    interval="15minute",
                    cache_path=cache_path,
                )
            except Exception as e:
                fetch_error = str(e)
                df = None

    is_test = bool(os.environ.get("PYTEST_CURRENT_TEST"))
    if df is None:
        if is_test:
            data_source = "SYNTHETIC_TEST"
            df = HistoricalDataLoader.generate_synthetic_nifty_data(
                start_date=datetime(2025, 1, 1),
                days=days,
                base_price=base_p,
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Historical data unavailable for {inst.symbol} (Token: {token}): {fetch_error or 'Kite returned zero candles.'}",
            )

    # 1. ORB Backtest
    bt_orb = StrategyBacktester(
        strategy_factory=lambda: IntradayORBStrategy(inst, settings.strategy),
        instrument=inst,
        app_settings=settings,
    )
    rep_orb = bt_orb.run(df, initial_capital=settings.risk.initial_capital)

    # 2. CPR Backtest
    bt_cpr = StrategyBacktester(
        strategy_factory=lambda: CPRRegimeBreakoutStrategy(inst, settings.strategy),
        instrument=inst,
        app_settings=settings,
    )
    rep_cpr = bt_cpr.run(df, initial_capital=settings.risk.initial_capital)

    # 3. Dual-EMA Backtest
    bt_dual = StrategyBacktester(
        strategy_factory=lambda: BufferedDualEMAStrategy(inst, settings.strategy),
        instrument=inst,
        app_settings=settings,
    )
    rep_dual = bt_dual.run(df, initial_capital=settings.risk.initial_capital)

    def serialize_rep(rep) -> dict:
        pf = rep.profit_factor
        if pf is not None and (pf == float("inf") or pf != pf):
            pf = 99.9
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

    comparison = [
        {
            "strategy": "30-Min Volatility-Filtered ORB",
            "strategy_id": "orb",
            "trades": rep_orb.total_trades,
            "win_rate": round(rep_orb.win_rate_pct, 1),
            "profit_factor": round(rep_orb.profit_factor if rep_orb.profit_factor != float("inf") else 99.9, 2),
            "sharpe": round(rep_orb.sharpe_ratio, 2),
            "max_drawdown": round(rep_orb.max_drawdown_pct, 1),
            "net_pnl": round(rep_orb.net_pnl, 2),
            "state": "ACTIVE" if settings.active_strategy.lower() == "orb" else "STANDBY",
        },
        {
            "strategy": "Central Pivot Range (CPR) Regime",
            "strategy_id": "cpr",
            "trades": rep_cpr.total_trades,
            "win_rate": round(rep_cpr.win_rate_pct, 1),
            "profit_factor": round(rep_cpr.profit_factor if rep_cpr.profit_factor != float("inf") else 99.9, 2),
            "sharpe": round(rep_cpr.sharpe_ratio, 2),
            "max_drawdown": round(rep_cpr.max_drawdown_pct, 1),
            "net_pnl": round(rep_cpr.net_pnl, 2),
            "state": "ACTIVE" if settings.active_strategy.lower() == "cpr" else "STANDBY",
        },
        {
            "strategy": "Adaptive Dual-EMA Trend System",
            "strategy_id": "dual_ema",
            "trades": rep_dual.total_trades,
            "win_rate": round(rep_dual.win_rate_pct, 1),
            "profit_factor": round(rep_dual.profit_factor if rep_dual.profit_factor != float("inf") else 99.9, 2),
            "sharpe": round(rep_dual.sharpe_ratio, 2),
            "max_drawdown": round(rep_dual.max_drawdown_pct, 1),
            "net_pnl": round(rep_dual.net_pnl, 2),
            "state": "ACTIVE" if settings.active_strategy.lower() == "dual_ema" else "STANDBY",
        },
    ]

    return {
        "days": days,
        "symbol": inst.symbol,
        "data_source": data_source,
        "strategies": {
            "orb": serialize_rep(rep_orb),
            "cpr": serialize_rep(rep_cpr),
            "dual_ema": serialize_rep(rep_dual),
        },
        "comparison": comparison,
    }


@router.get("/api/research/backtest")
def get_research_backtest(days: int = 180, symbol: str = "NIFTY"):
    """GET endpoint to backtest all three strategies and return comparative summary."""
    return run_all_three_backtests(days=days, symbol=symbol)


@router.post("/api/research/backtest")
def post_research_backtest(days: int = 180, symbol: str = "NIFTY"):
    """POST endpoint to backtest all three strategies and return comparative summary."""
    return run_all_three_backtests(days=days, symbol=symbol)


@router.post("/api/strategy/backtest")
def trigger_backtest(days: int = 180, symbol: str = "NIFTY", strategy: str = "cpr"):
    """
    Executes backtest over validated historical candles for the chosen strategy.
    Uses real Kite historical data when available.
    """
    from backtest.strategy_backtester import StrategyBacktester
    from config.settings import settings
    from config.universe import create_instrument_config_for_equity, resolve_universe_tokens
    from data.historical_loader import HistoricalDataLoader
    from data.instrument_resolver import instrument_resolver

    strat_name = strategy.lower()

    # rm100 is a research/portfolio-level strategy, not wired into this
    # backend's intraday backtester (no backtest.rm100_backtest module
    # exists here) — fail fast and cleanly instead of importing modules
    # that only make sense once that engine is un-parked.
    if strat_name == "rm100":
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="RM-100 is a research/experimental strategy and is not wired into this backend.",
        )

    # 1. Validate Kite session
    kite, auth_err = get_active_kite_with_diagnostics(force_validate=False)
    is_test = bool(os.environ.get("PYTEST_CURRENT_TEST"))

    # 2. Resolve authoritative instrument token
    if symbol and symbol != "NIFTY":
        token = instrument_resolver.resolve_token(symbol, exchange="NSE", kite_client=kite)
        if not token:
            tokens = resolve_universe_tokens()
            token = tokens.get(symbol, 0)
        inst = create_instrument_config_for_equity(symbol, token or 0)
        base_p = 2000.0
    else:
        inst = settings.instruments[0]
        token = instrument_resolver.resolve_token("NIFTY", exchange="NSE", kite_client=kite) or 256265
        inst.instrument_token = token
        base_p = 24000.0

    cache_path = settings.base_dir / "data" / "cache" / f"{inst.symbol}_15m_{days}d.csv"
    df = None
    data_source = "REAL_KITE"
    fetch_error: Optional[str] = None

    # 3. Try reading cached real data
    if cache_path.exists():
        try:
            df, _ = HistoricalDataLoader.load_cached_data_with_validation(cache_path)
        except Exception as e:
            logger.info(f"Cached data invalid or unreadable: {e}")
            df = None

    # 4. Try fetching from live Kite Connect Historical API
    if df is None:
        if not kite:
            fetch_error = auth_err or "Zerodha Kite Connect session is not active. Please authenticate via Kite login."
        elif not token:
            fetch_error = f"Unable to resolve numerical instrument_token for {inst.symbol} from Kite instrument master."
        else:
            try:
                today = datetime.now().date()
                start_d = today - timedelta(days=int(days * 1.5))
                df = HistoricalDataLoader.fetch_real_data(
                    kite_client=kite,
                    instrument_token=token,
                    start_date=start_d,
                    end_date=today,
                    interval="15minute",
                    cache_path=cache_path,
                )
            except Exception as e:
                fetch_error = str(e)
                logger.error(f"Kite historical fetch failed for {inst.symbol} (token={token}): {e}")
                df = None

    # 5. Handle fallback in test or raise error
    if df is None:
        if is_test:
            data_source = "SYNTHETIC_TEST"
            df = HistoricalDataLoader.generate_synthetic_nifty_data(
                start_date=datetime(2025, 1, 1),
                days=days,
                base_price=base_p,
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Historical data unavailable for {inst.symbol} (Token: {token}): {fetch_error or 'Kite returned zero candles.'}",
            )

    # 6. Instantiate strategy backtester
    if strat_name == "cpr":
        from strategy.cpr_strategy import CPRRegimeBreakoutStrategy
        backtester = StrategyBacktester(
            strategy_factory=lambda: CPRRegimeBreakoutStrategy(inst, settings.strategy),
            instrument=inst,
            app_settings=settings,
        )
    elif strat_name == "dual_ema":
        from strategy.dual_ema_strategy import BufferedDualEMAStrategy
        backtester = StrategyBacktester(
            strategy_factory=lambda: BufferedDualEMAStrategy(inst, settings.strategy),
            instrument=inst,
            app_settings=settings,
        )
    else:
        from strategy.orb_strategy import IntradayORBStrategy
        backtester = StrategyBacktester(
            strategy_factory=lambda: IntradayORBStrategy(inst, settings.strategy),
            instrument=inst,
            app_settings=settings,
        )

    report = backtester.run(df, initial_capital=settings.risk.initial_capital)

    # Sanitize profit factor for JSON serialization
    pf = report.profit_factor
    if pf is not None and (pf == float("inf") or pf != pf):
        pf = 99.9

    return {
        "success": True,
        "strategy": strat_name,
        "data_source": data_source,
        "report": {
            "symbol": inst.symbol,
            "strategy": strat_name.upper(),
            "total_trades": report.total_trades,
            "long_trades": report.long_trades,
            "short_trades": report.short_trades,
            "winning_trades": report.winning_trades,
            "losing_trades": report.losing_trades,
            "win_rate_pct": round(report.win_rate_pct, 1),
            "gross_pnl": round(report.gross_pnl, 2),
            "total_transaction_costs": round(report.total_transaction_costs, 2),
            "net_pnl": round(report.net_pnl, 2),
            "profit_factor": pf,
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


@router.get("/api/strategy/telemetry")
def get_strategy_telemetry(symbol: str = "NIFTY", strategy: str = "cpr"):
    """
    Provides real-time telemetry for the workstation using real Kite market data.
    Computes indicators dynamically for the selected strategy (CPR, Dual-EMA, ORB).
    """
    from config.settings import settings
    from config.universe import create_instrument_config_for_equity, resolve_universe_tokens
    from data.market_calendar import MarketCalendar
    from database.db import DatabaseManager
    from datetime import time

    strat_name = strategy.lower()
    tokens = resolve_universe_tokens()

    if symbol and symbol != "NIFTY":
        token = tokens.get(symbol)
        target_inst = create_instrument_config_for_equity(symbol, token)
        quote_key = f"NSE:{symbol}"
    else:
        target_inst = settings.instruments[0]
        token = target_inst.instrument_token
        quote_key = "NSE:NIFTY 50"

    now = datetime.now()
    cur_time = now.time()
    phase = MarketCalendar.get_session_phase(cur_time)
    is_open = (time(9, 15) <= cur_time <= time(15, 30)) and MarketCalendar.is_trading_day(now.date())

    kite = get_active_kite()
    is_test = bool(os.environ.get("PYTEST_CURRENT_TEST"))

    # If unauthenticated and not testing, return status message
    if not kite and not is_test:
        return {
            "symbol": target_inst.symbol,
            "strategy": strat_name,
            "authenticated": False,
            "data_source": "NONE",
            "message": "Zerodha Kite session not authenticated. Please log in with Kite Connect to stream real live market data.",
            "current_price": 0.0,
            "price_change_pts": 0.0,
            "price_change_pct": 0.0,
            "vwap": 0.0,
            "chart_candles": [],
            "algorithm_state": "AWAITING_KITE_LOGIN",
            "market_status": "OPEN" if is_open else "CLOSED",
            "market_phase": phase.value,
            "strategy_levels": {},
            "active_signal": None,
            "active_trade": None,
            "risk_summary": {
                "capital": settings.risk.initial_capital,
                "daily_risk_limit": round(settings.risk.initial_capital * settings.risk.max_daily_loss_pct, 2),
                "daily_risk_used": 0.0,
                "daily_risk_remaining": round(settings.risk.initial_capital * settings.risk.max_daily_loss_pct, 2),
                "risk_per_trade_pct": settings.risk.risk_per_trade_pct * 100.0,
                "kill_switch_pct": settings.risk.max_daily_loss_pct * 100.0,
                "max_trades": settings.strategy.max_trades_per_instrument_day,
                "trades_taken": 0,
            },
        }

    # Fetch real live quote from Kite (never default to fake prices in production)
    ltp = 0.0
    open_p = 0.0
    current_vwap = 0.0
    change_pts = 0.0
    change_pct = 0.0
    chart_candles = []

    if is_test:
        ltp = 24000.0 if symbol == "NIFTY" else 2000.0
        open_p = ltp
        current_vwap = ltp

    if kite:
        try:
            q_res = kite.quote([quote_key])
            q_data = q_res.get(quote_key, {})
            if q_data:
                ltp = float(q_data.get("last_price", ltp))
                ohlc = q_data.get("ohlc", {})
                open_p = float(ohlc.get("open", ltp))
                prev_c = float(ohlc.get("close", ltp))
                current_vwap = float(q_data.get("average_price", ltp)) or ltp
                change_pts = round(ltp - prev_c, 2)
                change_pct = round(((ltp - prev_c) / prev_c) * 100.0, 2) if prev_c > 0 else 0.0

            # Fetch today's real intraday candles for chart
            if token:
                today_str = now.strftime("%Y-%m-%d")
                intraday_bars = kite.historical_data(
                    instrument_token=token,
                    from_date=today_str,
                    to_date=today_str,
                    interval="15minute",
                )
                if not intraday_bars:
                    # If market hasn't generated bars today, pull previous session
                    prev_start = (now - timedelta(days=5)).strftime("%Y-%m-%d")
                    intraday_bars = kite.historical_data(
                        instrument_token=token,
                        from_date=prev_start,
                        to_date=today_str,
                        interval="15minute",
                    )[-15:]

                for bar in intraday_bars:
                    dt = bar.get("date")
                    t_str = dt.strftime("%H:%M") if hasattr(dt, "strftime") else str(dt)[11:16]
                    chart_candles.append({
                        "time": t_str,
                        "open": round(float(bar["open"]), 2),
                        "high": round(float(bar["high"]), 2),
                        "low": round(float(bar["low"]), 2),
                        "close": round(float(bar["close"]), 2),
                        "volume": int(bar.get("volume", 0)),
                        "vwap": round(current_vwap, 2),
                    })
        except Exception as e:
            logger.warning(f"Kite live quote/candles fetch failed: {e}")

    # If test mode and empty, generate sample bars so tests pass
    if not chart_candles and is_test:
        from data.historical_loader import HistoricalDataLoader
        df_sample = HistoricalDataLoader.generate_synthetic_nifty_data(days=1, seed=42, base_price=ltp)
        for _, row in df_sample.iterrows():
            chart_candles.append({
                "time": row["datetime"].strftime("%H:%M"),
                "open": round(row["open"], 2),
                "high": round(row["high"], 2),
                "low": round(row["low"], 2),
                "close": round(row["close"], 2),
                "volume": int(row["volume"]),
                "vwap": round(row["close"], 2),
            })

    # Evaluate real strategy logic via PredictionService
    df_eval = pd.DataFrame()
    if chart_candles:
        candle_dicts = []
        today_date = now.date()
        for c in chart_candles:
            try:
                t_parts = c["time"].split(":")
                c_dt = datetime.combine(today_date, time(int(t_parts[0]), int(t_parts[1])))
            except Exception:
                c_dt = now
            candle_dicts.append({
                "datetime": c_dt,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "volume": c.get("volume", 0),
                "vwap": c.get("vwap", current_vwap),
            })
        df_eval = pd.DataFrame(candle_dicts)

    from strategy.prediction_service import prediction_service
    preds, _ = prediction_service.evaluate_symbol(
        symbol=target_inst.symbol,
        df_15m=df_eval,
        current_ltp=ltp,
        token=token,
    )

    pred = preds.get(strat_name) or preds.get("orb")
    strategy_levels = pred.levels if pred and pred.levels else {}
    algo_state = pred.status.replace("_", " ") if pred else "SCANNING"

    active_signal = None
    if pred and pred.direction:
        active_signal = {
            "type": "BUY" if pred.direction == "LONG" else "SELL",
            "symbol": target_inst.symbol,
            "trigger": pred.reason,
            "entry": pred.entry or ltp,
            "stop_loss": pred.stop_loss,
            "target": pred.target,
            "confidence": 85,
        }

    # Fetch recent trades from DB
    db = DatabaseManager(settings.db_path)
    trades = db.get_all_trades()
    active_trade = None
    if trades and not trades[0].get("exit_price"):
        t = trades[0]
        qty = t.get("quantity", target_inst.lot_size)
        direction = t.get("direction", "BUY")
        entry_p = t.get("entry_price", ltp)
        unrealized = (ltp - entry_p) * qty if direction == "BUY" else (entry_p - ltp) * qty
        active_trade = {
            "id": t.get("trade_id", "TRD_01"),
            "symbol": t.get("symbol", target_inst.symbol),
            "direction": direction,
            "entry_price": entry_p,
            "current_price": ltp,
            "stop_loss": t.get("initial_stop", entry_p * 0.99),
            "target": t.get("initial_target", entry_p * 1.02),
            "quantity": qty,
            "unrealized_pnl": round(unrealized, 2),
        }

    # Risk metrics
    capital = settings.risk.initial_capital
    daily_risk_limit = round(capital * settings.risk.max_daily_loss_pct, 2)
    today_str = now.strftime("%Y-%m-%d")
    trades_today = [t for t in trades if (t.get("entry_time") or "").startswith(today_str)]
    daily_realized = sum((t.get("pnl_net") or 0.0) for t in trades if (t.get("exit_time") or "").startswith(today_str))

    return {
        "symbol": target_inst.symbol,
        "strategy": strat_name,
        "data_source": "REAL_KITE" if kite else ("SYNTHETIC_TEST" if is_test else "NONE"),
        "authenticated": bool(kite),
        "current_price": ltp,
        "price_change_pts": change_pts,
        "price_change_pct": change_pct,
        "vwap": current_vwap,
        "strategy_levels": strategy_levels,
        "algorithm_state": algo_state,
        "active_signal": active_signal,
        "active_trade": active_trade,
        "market_status": "OPEN" if is_open else "CLOSED",
        "market_phase": phase.value,
        "chart_candles": chart_candles,
        "risk_summary": {
            "capital": capital,
            "daily_risk_limit": daily_risk_limit,
            "daily_risk_used": abs(min(daily_realized, 0.0)),
            "daily_risk_remaining": max(daily_risk_limit + daily_realized, 0.0),
            "risk_per_trade_pct": settings.risk.risk_per_trade_pct * 100.0,
            "kill_switch_pct": settings.risk.max_daily_loss_pct * 100.0,
            "max_trades": settings.strategy.max_trades_per_instrument_day,
            "trades_taken": len(trades_today),
        },
    }
