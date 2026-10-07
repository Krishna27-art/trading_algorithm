"""
Kite Connect signal dashboard — FastAPI app entrypoint (READ-ONLY / SIGNAL-ONLY).

The user executes trades manually; nothing in this API places, modifies or
cancels orders.

Routers:
  backend/kite.py              - /kite/*   (OAuth login, status, logout)
  backend/market.py            - /api/market/prices
  backend/stream_routes.py     - /api/stream/*  (canonical live path)
  backend/signals.py           - /api/strategy/*  (state, scanner, telemetry)
  backend/backtest_routes.py   - /api/research/backtest, /api/strategy/backtest
                                 (offline research; secret-protected; optional)
  backend/system.py            - /api/system/health

Run with: uvicorn backend.main:app --reload --port 8000
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.routes.kite import router as kite_router
from backend.routes.market import router as market_router
from backend.routes.signals import router as signals_router
from backend.routes.stream import router as stream_router
from backend.routes.system import router as system_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backend_api")

_DEFAULT_ORIGINS = (
    "http://localhost:5173,http://127.0.0.1:5173,"
    "http://localhost:3000,http://127.0.0.1:3000"
)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def get_allowed_origins() -> list:
    """Explicit origin allow-list. Override with CORS_ORIGINS=a,b,c (never '*')."""
    raw = os.getenv("CORS_ORIGINS", _DEFAULT_ORIGINS)
    return [o.strip() for o in raw.split(",") if o.strip() and o.strip() != "*"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The legacy daily history warmer used to start at *import time* (a side
    # effect of merely importing backend.main). It belongs to the legacy
    # scanner/research path, not the canonical KiteTicker path, so it is now
    # OFF by default and, if explicitly enabled, starts in app startup only.
    if _env_flag("ENABLE_LEGACY_HISTORY_WARMER", False):
        try:
            from backend.scanner.history_context_warmer import start_daily_history_warmer

            start_daily_history_warmer()
            logger.warning("Legacy history warmer enabled via ENABLE_LEGACY_HISTORY_WARMER.")
        except Exception:
            logger.exception("Legacy history warmer failed to start")
    try:
        yield
    finally:
        try:
            from backend.streaming.market_stream_manager import market_stream_manager
            market_stream_manager.stop_stream()
            logger.info("Market stream manager stopped during application shutdown.")
        except Exception:
            logger.exception("Error stopping market stream manager during shutdown")


app = FastAPI(
    title="Kite Connect Signal Dashboard API",
    description="Read-only backend API for Kite login, live stream state, and strategy signals.",
    version="2.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Shared-Secret"],
)

app.include_router(kite_router)
app.include_router(market_router)
app.include_router(stream_router)
app.include_router(signals_router)
app.include_router(system_router)

# Backtests are offline research: isolated in their own router, protected by
# the shared secret, and can be switched off entirely in production.
if _env_flag("ENABLE_BACKTEST_ROUTES", True):
    from backend.routes.backtest import router as backtest_router

    app.include_router(backtest_router)
