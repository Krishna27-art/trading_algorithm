"""
Zerodha Kite Connect Trading Dashboard — FastAPI app entrypoint.

This used to be a single 1559-line file. It's now just app setup + CORS +
router registration; every route lives in its own module:

  backend/kite.py       - /kite/*, /api/profile, /api/margins, /api/logout
  backend/signals.py    - /api/strategy/*, /api/research/*
  backend/system.py     - /api/system/health

Also removed here: POST /api/login-url, POST /api/login, GET /api/status.
All three were dead legacy routes the frontend never called (see
backend/kite.py's module docstring for why).

Run with: uvicorn backend.main:app --reload --port 8000
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.kite import router as kite_router
from backend.market import router as market_router
from backend.signals import router as signals_router
from backend.stream import router as stream_router
from backend.system import router as system_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backend_api")

app = FastAPI(
    title="Zerodha Kite Connect Trading Dashboard API",
    description="Backend API for Zerodha Kite Connect login, session management, and strategy signals.",
    version="2.0.0",
)

# Configure CORS for local development with Vite (strictly permitted origins only)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(kite_router)
app.include_router(market_router)
app.include_router(signals_router)
app.include_router(stream_router)
app.include_router(system_router)

from scanner.history_context_warmer import (
    start_daily_history_warmer,
)

start_daily_history_warmer()
