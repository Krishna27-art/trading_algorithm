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
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException, status

from broker.kite_adapter import get_active_kite, get_active_kite_with_diagnostics, kite_broker_adapter
from data.time_utils import now_ist_iso, now_ist_naive, today_ist

logger = logging.getLogger("backend_api.signals")

router = APIRouter()

# Global in-memory cache for live research predictions to prevent expensive repetitive calculations
_live_research_cache: Dict[str, Any] = {
    "timestamp": 0.0,
    "top_n": 10,
    "data": None,
}
_live_research_cache_lock = threading.Lock()

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
    "apex": {
        "name": "APEX-AIVEM Pre-Market Catalyst & Momentum Engine",
        "description": "Composite score (gap/ATR, RVOL, VWAP, candle imbalance) with optional VIX/GIFT-Nifty/sector context",
        "timeframe": "15m candles",
        "key_levels": ["VWAP", "ATR Stop", "ATR Target", "Gap/ATR Ratio"],
    },
    "sector_impulse": {
        "name": "Sector Impulse Transmission (SIT)",
        "description": "Cross-sectional lead-lag transmission trading lagging peers after idiosyncratic volume shock in sector leader",
        "timeframe": "15m candles",
        "key_levels": ["Leader Return", "Idiosyncratic Shock", "Gap Sigma", "Time Stop"],
    },
    "ssf_l5_srm": {
        "name": "Single-Stock Futures L5 Microprice & Sector Residual Momentum (SSF-L5-SRM)",
        "description": "Level-5 order flow imbalance, microprice drift, futures basis z-score, and sector residual momentum",
        "timeframe": "Tick / L5 Depth / 15m Regime",
        "key_levels": ["Microprice Dev", "Basis Z-Score", "OFI Imbalance", "Parkinson Vol"],
    },
}


@router.get("/api/strategy/state")
def get_strategy_state(strategy: Optional[str] = None):
    """Returns configuration, state, and safety parameters of the selected strategy."""
    from config.settings import settings
    from data.market_calendar import MarketCalendar

    now = now_ist_naive()
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
    """Fetches live executed trades from the SQLite journal."""
    from database.db import DatabaseManager
    from config.settings import settings

    db = DatabaseManager(settings.db_path)
    trades = db.get_live_trades()

    return {
        "trades": trades,
        "count": len(trades),
    }


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

    kite = get_active_kite()

    if not kite:
        return {
            "status": "AUTH_REQUIRED",
            "data_source": "NONE",
            "message": "Zerodha Kite Connect session is not authenticated. Please log in with Kite to scan real market quotes.",
            "candidates": [],
            "count": 0,
            "top_n": top_n,
            "timestamp": now_ist_iso(),
        }

    try:
        scanner = StockUniverseScanner()
        ranked, data_source = scanner.scan_universe(
            kite_client=kite,
            top_n=top_n,
            force_refresh_history=refresh,
            allow_synthetic=False,
        )
        summary = getattr(scanner, "last_pipeline_summary", {})
        return {
            "status": "success",
            "data_source": data_source,
            "timestamp": now_ist_iso(),
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

    now = now_ist_naive()
    cur_time = now.time()
    is_open = (dt_time(9, 15) <= cur_time <= dt_time(15, 30)) and MarketCalendar.is_trading_day(now.date())
    kite, auth_err = get_active_kite_with_diagnostics(force_validate=False)

    if not kite:
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
    with _live_research_cache_lock:
        cache_age = time_mod.time() - _live_research_cache["timestamp"]
        if not force_refresh and cache_age < 15.0 and _live_research_cache["data"] and _live_research_cache["top_n"] == top_n:
            return _live_research_cache["data"]

    try:
        scanner = StockUniverseScanner()
        ranked_metrics, data_source_label = scanner.scan_universe(
            kite_client=kite,
            top_n=top_n,
            allow_synthetic=False,
        )

        candidates: List[CandidatePrediction] = []
        cache_dir = settings.base_dir / "data" / "cache"
        today = now.date()

        for idx, item in enumerate(ranked_metrics, start=1):
            sym = item.symbol
            token = item.token
            ltp = item.ltp
            df_15m = None

            cache_file = cache_dir / f"{sym}_15m.csv"

            try:
                df_15m = (
                    HistoricalDataLoader
                    .load_or_refresh_intraday_cache(
                        kite_client=kite,
                        instrument_token=token,
                        cache_path=cache_file,
                        now=now,
                        lookback_days=45,
                        interval="15minute",
                    )
                )
            except Exception as e:
                logger.debug(
                    f"Could not load fresh 15m bars for "
                    f"{sym} from Kite: {e}"
                )
                df_15m = None

            # In production: if real intraday data is unavailable, skip candidate or mark unavailable (never fake data)
            if df_15m is None or df_15m.empty:
                logger.warning(f"Real 15m intraday data unavailable for {sym} (Token {token}) — skipping.")
                continue

            # Extract real Level-5 depth snapshot if available from live stream or quote
            book_snap = kite_broker_adapter.get_latest_book_snapshot(sym)

            preds, consensus = prediction_service.evaluate_symbol(
                symbol=sym,
                df_15m=df_15m,
                current_ltp=ltp,
                token=token,
                stock_metric=item,
                book_snapshot=book_snap,
                kite_client=kite,
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

        if not candidates:
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
                "data_source": "REAL_KITE" if data_source_label == "REAL" else "NONE",
                "timestamp": now.isoformat(),
                "market_status": "OPEN" if is_open else "CLOSED",
                "scanned_count": summary.get("universe_count", 300),
                "returned_count": len(candidates),
                "candidates": [c.to_dict() for c in candidates],
                "key_insights": key_insights,
            }

        with _live_research_cache_lock:
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
                today = today_ist()
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

    if df is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Historical data unavailable for {inst.symbol} "
                f"(Token: {token}): "
                f"{fetch_error or 'Kite returned zero candles.'}"
            ),
        )

    # 1. ORB Backtest
    bt_orb = StrategyBacktester(
        strategy_factory=lambda: IntradayORBStrategy(inst, settings.strategy),
        instrument=inst,
        app_settings=settings,
        persist_trades=False,
    )
    rep_orb = bt_orb.run(df, initial_capital=settings.risk.initial_capital)

    # 2. CPR Backtest
    bt_cpr = StrategyBacktester(
        strategy_factory=lambda: CPRRegimeBreakoutStrategy(inst, settings.strategy),
        instrument=inst,
        app_settings=settings,
        persist_trades=False,
    )
    rep_cpr = bt_cpr.run(df, initial_capital=settings.risk.initial_capital)

    # 3. Dual-EMA Backtest
    bt_dual = StrategyBacktester(
        strategy_factory=lambda: BufferedDualEMAStrategy(inst, settings.strategy),
        instrument=inst,
        app_settings=settings,
        persist_trades=False,
    )
    rep_dual = bt_dual.run(df, initial_capital=settings.risk.initial_capital)

    # 4. APEX Backtest
    from strategy.apex_engine import ApexStrategy
    bt_apex = StrategyBacktester(
        strategy_factory=lambda: ApexStrategy(inst, settings.strategy),
        instrument=inst,
        app_settings=settings,
        persist_trades=False,
    )
    rep_apex = bt_apex.run(df, initial_capital=settings.risk.initial_capital)

    # 5. Sector Impulse Backtest
    from data.sector_peer_manager import SectorPeerManager
    from strategy.sector_impulse_strategy import SectorImpulseStrategy
    ctx_sit = SectorPeerManager.build_peer_context(inst.symbol, kite_client=kite)
    bt_sit = StrategyBacktester(
        strategy_factory=lambda: SectorImpulseStrategy(inst, settings.strategy, ctx=ctx_sit),
        instrument=inst,
        app_settings=settings,
        persist_trades=False,
    )
    rep_sit = bt_sit.run(df, initial_capital=settings.risk.initial_capital)

    # 6. SSF-L5-SRM Backtest
    # Note: SSF-L5-SRM is an order-book tick strategy (Level-5 depth). 
    # Backtesting on 15m OHLCV tests regime gate tracking; historical depth is not present in OHLCV.
    from strategy.ssf_l5_srm_strategy import SsfL5SrmStrategy
    bt_ssf = StrategyBacktester(
        strategy_factory=lambda: SsfL5SrmStrategy(inst, settings.strategy),
        instrument=inst,
        app_settings=settings,
        persist_trades=False,
    )
    rep_ssf = bt_ssf.run(df, initial_capital=settings.risk.initial_capital)

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
        {
            "strategy": "APEX-AIVEM Catalyst & Momentum",
            "strategy_id": "apex",
            "trades": rep_apex.total_trades,
            "win_rate": round(rep_apex.win_rate_pct, 1),
            "profit_factor": round(rep_apex.profit_factor if rep_apex.profit_factor != float("inf") else 99.9, 2),
            "sharpe": round(rep_apex.sharpe_ratio, 2),
            "max_drawdown": round(rep_apex.max_drawdown_pct, 1),
            "net_pnl": round(rep_apex.net_pnl, 2),
            "state": "ACTIVE" if settings.active_strategy.lower() == "apex" else "STANDBY",
        },
        {
            "strategy": "Sector Impulse Transmission (SIT)",
            "strategy_id": "sector_impulse",
            "trades": rep_sit.total_trades,
            "win_rate": round(rep_sit.win_rate_pct, 1),
            "profit_factor": round(rep_sit.profit_factor if rep_sit.profit_factor != float("inf") else 99.9, 2),
            "sharpe": round(rep_sit.sharpe_ratio, 2),
            "max_drawdown": round(rep_sit.max_drawdown_pct, 1),
            "net_pnl": round(rep_sit.net_pnl, 2),
            "state": "ACTIVE" if settings.active_strategy.lower() in ("sector_impulse", "sit") else "STANDBY",
        },
        {
            "strategy": "SSF-L5-SRM Microprice & Residual Momentum",
            "strategy_id": "ssf_l5_srm",
            "trades": rep_ssf.total_trades,
            "win_rate": round(rep_ssf.win_rate_pct, 1),
            "profit_factor": round(rep_ssf.profit_factor if rep_ssf.profit_factor != float("inf") else 99.9, 2),
            "sharpe": round(rep_ssf.sharpe_ratio, 2),
            "max_drawdown": round(rep_ssf.max_drawdown_pct, 1),
            "net_pnl": round(rep_ssf.net_pnl, 2),
            "state": "ACTIVE" if settings.active_strategy.lower() in ("ssf_l5_srm", "ssf") else "STANDBY",
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
            "apex": serialize_rep(rep_apex),
            "sector_impulse": serialize_rep(rep_sit),
            "ssf_l5_srm": serialize_rep(rep_ssf),
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
    # backend's intraday backtester — fail fast and cleanly.
    if strat_name == "rm100":
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="RM-100 is a research/experimental strategy and is not wired into this backend.",
        )

    # 1. Validate Kite session
    kite, auth_err = get_active_kite_with_diagnostics(force_validate=False)

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
                today = today_ist()
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Historical data unavailable for {inst.symbol} "
                f"(Token: {token}): "
                f"{fetch_error or 'Kite returned zero candles.'}"
            ),
        )

    # 6. Instantiate strategy backtester
    if strat_name == "cpr":
        from strategy.cpr_strategy import CPRRegimeBreakoutStrategy
        backtester = StrategyBacktester(
            strategy_factory=lambda: CPRRegimeBreakoutStrategy(inst, settings.strategy),
            instrument=inst,
            app_settings=settings,
            persist_trades=False,
        )
    elif strat_name == "dual_ema":
        from strategy.dual_ema_strategy import BufferedDualEMAStrategy
        backtester = StrategyBacktester(
            strategy_factory=lambda: BufferedDualEMAStrategy(inst, settings.strategy),
            instrument=inst,
            app_settings=settings,
            persist_trades=False,
        )
    elif strat_name == "apex":
        from strategy.apex_engine import ApexStrategy
        backtester = StrategyBacktester(
            strategy_factory=lambda: ApexStrategy(inst, settings.strategy),
            instrument=inst,
            app_settings=settings,
            persist_trades=False,
        )
    elif strat_name in ("sector_impulse", "sit"):
        from data.sector_peer_manager import SectorPeerManager
        from strategy.sector_impulse_strategy import SectorImpulseStrategy
        ctx_sit = SectorPeerManager.build_peer_context(inst.symbol, kite_client=kite)
        backtester = StrategyBacktester(
            strategy_factory=lambda: SectorImpulseStrategy(inst, settings.strategy, ctx=ctx_sit),
            instrument=inst,
            app_settings=settings,
            persist_trades=False,
        )
    elif strat_name in ("ssf_l5_srm", "ssf"):
        from strategy.ssf_l5_srm_strategy import SsfL5SrmStrategy
        backtester = StrategyBacktester(
            strategy_factory=lambda: SsfL5SrmStrategy(inst, settings.strategy),
            instrument=inst,
            app_settings=settings,
            persist_trades=False,
        )
    else:
        from strategy.orb_strategy import IntradayORBStrategy
        backtester = StrategyBacktester(
            strategy_factory=lambda: IntradayORBStrategy(inst, settings.strategy),
            instrument=inst,
            app_settings=settings,
            persist_trades=False,
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
        # Fix 1.4: NIFTY instrument_token is None by default in settings.
        # Always resolve it via InstrumentResolver or fall back to the known
        # NSE integer token (256265) rather than passing None to historical_data().
        from data.instrument_resolver import instrument_resolver
        kite_pre = get_active_kite()
        token = (
            instrument_resolver.resolve_token("NIFTY", exchange="NSE", kite_client=kite_pre)
            or 256265
        )
        target_inst.instrument_token = token
        if not target_inst.max_risk_cap or target_inst.max_risk_cap <= 0:
            target_inst.max_risk_cap = 80.0
        quote_key = "NSE:NIFTY 50"

    now = now_ist_naive()
    cur_time = now.time()
    phase = MarketCalendar.get_session_phase(cur_time)
    is_open = (time(9, 15) <= cur_time <= time(15, 30)) and MarketCalendar.is_trading_day(now.date())

    kite = get_active_kite()

    # If unauthenticated, return status message
    if not kite:
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
    q_data: dict = {}

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
                if ltp > 0 and (not target_inst.max_risk_cap or target_inst.max_risk_cap <= 0):
                    target_inst.max_risk_cap = round(ltp * 0.004, 2)

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
                    # Market hasn't generated bars today (pre-market / holiday).
                    # Pull the last session's bars — but keep their REAL dates so
                    # strategies don't think they are replaying today.
                    prev_start = (now - timedelta(days=5)).strftime("%Y-%m-%d")
                    intraday_bars = kite.historical_data(
                        instrument_token=token,
                        from_date=prev_start,
                        to_date=today_str,
                        interval="15minute",
                    )[-15:]

                for bar in intraday_bars:
                    # Fix 1.5: preserve the original bar datetime — do NOT
                    # re-stamp it with today's date if it comes from a previous
                    # session. Strategies must see the real candle date.
                    dt = bar.get("date")
                    if hasattr(dt, "strftime"):
                        t_str = dt.strftime("%H:%M")
                        bar_date = dt.strftime("%Y-%m-%d")
                    else:
                        dt_str = str(dt)
                        t_str = dt_str[11:16]
                        bar_date = dt_str[:10]
                    chart_candles.append({
                        "time": t_str,
                        "date": bar_date,  # real date — never fabricated
                        "open": round(float(bar["open"]), 2),
                        "high": round(float(bar["high"]), 2),
                        "low": round(float(bar["low"]), 2),
                        "close": round(float(bar["close"]), 2),
                        "volume": int(bar.get("volume", 0)),
                        "vwap": round(current_vwap, 2),
                    })
        except Exception as e:
            logger.warning(f"Kite live quote/candles fetch failed: {e}")

    # Evaluate real strategy logic via PredictionService
    df_eval = pd.DataFrame()
    if chart_candles:
        candle_dicts = []
        for c in chart_candles:
            try:
                # Fix 1.5: use the real bar date preserved in the "date" field,
                # not today's date. This prevents previous-session candles from
                # appearing to belong to today when strategies evaluate them.
                bar_date_str = c.get("date", now.strftime("%Y-%m-%d"))
                t_parts = c["time"].split(":")
                from datetime import date as dt_date
                bar_date = dt_date.fromisoformat(bar_date_str)
                c_dt = datetime.combine(bar_date, time(int(t_parts[0]), int(t_parts[1])))
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
    book_snap = kite_broker_adapter.get_latest_book_snapshot(target_inst.symbol)
    if book_snap is None and q_data:
        book_snap = kite_broker_adapter.make_book_snapshot_from_quote(q_data, target_inst.symbol)

    preds, consensus = prediction_service.evaluate_symbol(
        symbol=target_inst.symbol,
        df_15m=df_eval,
        current_ltp=ltp,
        token=token,
        book_snapshot=book_snap,
        kite_client=kite,
    )

    pred = preds.get(strat_name) or preds.get("orb")
    strategy_levels = pred.levels if pred and pred.levels else {}
    algo_state = pred.status.replace("_", " ") if pred else "SCANNING"

    # Consensus agreement is a vote fraction, not a probability.
    consensus_agreement_pct = consensus.get("consensus_agreement_pct")

    # This field describes whether the selected strategy currently has a
    # directional output. It is not a probability and should not be labeled
    # as confidence.
    strategy_has_direction = bool(pred and pred.direction is not None)

    # Fetch recent trades from DB
    db = DatabaseManager(settings.db_path)
    trades = db.get_live_trades()
    active_trade = None
    if trades and not trades[0].get("exit_price"):
        t = trades[0]
        qty = t.get("quantity")  # Fix 2.9: no fabricated fallback
        direction = t.get("direction")
        entry_p = t.get("entry_price")
        # Only build active_trade card when all essential fields are real DB values
        if qty and direction and entry_p:
            unrealized = (ltp - entry_p) * qty if direction == "BUY" else (entry_p - ltp) * qty
            active_trade = {
                "id": t.get("trade_id"),          # None if missing — not "TRD_01"
                "symbol": t.get("symbol"),
                "direction": direction,
                "entry_price": entry_p,
                "current_price": ltp,
                "stop_loss": t.get("initial_stop"),   # None if missing — not entry*0.99
                "target": t.get("initial_target"),    # None if missing — not entry*1.02
                "quantity": qty,
                "unrealized_pnl": round(unrealized, 2),
            }

    # Risk metrics & pre-trade risk validation
    from risk.risk_manager import RiskManager
    capital = settings.risk.initial_capital
    daily_risk_limit = round(capital * settings.risk.max_daily_loss_pct, 2)
    today_str = now.strftime("%Y-%m-%d")
    trades_today = [t for t in trades if (t.get("entry_time") or "").startswith(today_str)]
    daily_realized = sum((t.get("pnl_net") or 0.0) for t in trades if (t.get("exit_time") or "").startswith(today_str))

    risk_mgr = RiskManager(
        risk_config=settings.risk,
        max_portfolio_daily_trades=settings.strategy.max_trades_per_instrument_day,
    )
    risk_mgr.reset_daily_state(now.date())
    for tr in trades_today:
        sym = tr.get("symbol", target_inst.symbol)
        risk_mgr.daily_trades_count[sym] = risk_mgr.daily_trades_count.get(sym, 0) + 1
    risk_mgr.update_pnl(realized_pnl_delta=daily_realized, current_unrealized_pnl=0.0, capital=capital)

    is_risk_approved, risk_reason = risk_mgr.validate_pre_trade(
        symbol=target_inst.symbol,
        current_time=cur_time,
        quantity=target_inst.lot_size,
        capital=capital,
        has_open_position=bool(active_trade),
    )

    active_signal = None
    if pred and pred.direction:
        active_signal = {
            "type": "BUY" if pred.direction == "LONG" else "SELL",
            "symbol": target_inst.symbol,
            "trigger": pred.reason,
            "entry": pred.entry or ltp,
            "stop_loss": pred.stop_loss,
            "target": pred.target,
            "consensus_agreement_pct": consensus_agreement_pct,
            "risk_approved": is_risk_approved,
            "risk_rejection_reason": risk_reason if not is_risk_approved else None,
        }

    return {
        "symbol": target_inst.symbol,
        "strategy": strat_name,
        "data_source": "REAL_KITE" if kite else "NONE",
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
