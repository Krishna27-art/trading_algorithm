import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from kiteconnect import KiteConnect
from pydantic import BaseModel, Field
from dotenv import set_key

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backend_api")

BASE_DIR = Path(__file__).resolve().parent.parent
TOKEN_FILE = BASE_DIR / "session_token.json"
ENV_FILE = BASE_DIR / ".env"

app = FastAPI(
    title="Zerodha Kite Connect Trading Dashboard API",
    description="Backend API for Zerodha Kite Connect login, session management, and user profile",
    version="1.0.0",
)

# Configure CORS for local development with Vite
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "*"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global in-memory kite client cache for fast local responses
_cached_kite: Optional[KiteConnect] = None


class LoginRequest(BaseModel):
    api_key: str = Field(..., min_length=1, description="Zerodha Kite API Key")
    api_secret: str = Field(..., min_length=1, description="Zerodha Kite API Secret")
    request_token: str = Field(..., min_length=1, description="Request token from redirect URL")


class LoginUrlRequest(BaseModel):
    api_key: Optional[str] = None


def get_saved_session() -> Optional[Dict[str, Any]]:
    if not TOKEN_FILE.exists():
        return None
    try:
        with open(TOKEN_FILE, "r") as f:
            data = json.load(f)
            if data.get("access_token") and data.get("api_key"):
                return data
    except Exception as e:
        logger.warning(f"Failed to read session file: {e}")
    return None


def get_active_kite() -> Optional[KiteConnect]:
    global _cached_kite
    if _cached_kite:
        return _cached_kite

    session = get_saved_session()
    if not session:
        return None

    try:
        kite = KiteConnect(api_key=session["api_key"])
        kite.set_access_token(session["access_token"])
        # Quick validation check with profile
        profile = kite.profile()
        if profile and "user_id" in profile:
            _cached_kite = kite
            return kite
    except Exception as e:
        logger.warning(f"Saved session token is invalid or expired: {e}")
        # Clear invalid session
        if TOKEN_FILE.exists():
            try:
                TOKEN_FILE.unlink()
            except Exception:
                pass
        _cached_kite = None

    return None


@app.get("/api/status")
def check_status():
    """Checks if there is an active, valid session on the backend."""
    session = get_saved_session()
    if not session:
        return {"authenticated": False}

    kite = get_active_kite()
    if not kite:
        return {"authenticated": False, "message": "Session expired or invalid"}

    return {
        "authenticated": True,
        "user": {
            "user_id": session.get("user_id", ""),
            "user_name": session.get("user_name", "Trader"),
            "login_time": session.get("login_time", ""),
            "api_key": session.get("api_key", "")[:4] + "****" if session.get("api_key") else "",
        },
    }


@app.post("/api/login-url")
def generate_login_url(req: LoginUrlRequest):
    """Generates the Zerodha OAuth login URL for the given API Key."""
    api_key = req.api_key
    if not api_key:
        session = get_saved_session()
        if session and session.get("api_key"):
            api_key = session.get("api_key")
        elif os.getenv("KITE_API_KEY"):
            api_key = os.getenv("KITE_API_KEY")

    if not api_key or api_key == "your_api_key_here":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="API Key is required to generate the login URL.",
        )

    kite = KiteConnect(api_key=api_key.strip())
    return {"login_url": kite.login_url()}


@app.post("/api/login")
def login(req: LoginRequest):
    """
    Exchanges the request token for an access token.
    Saves token securely on the backend only.
    Never exposes raw access_token to the client.
    """
    global _cached_kite
    api_key = req.api_key.strip()
    api_secret = req.api_secret.strip()
    request_token = req.request_token.strip()

    # Extract request_token if user pasted the entire redirect URL
    if "request_token=" in request_token:
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(request_token)
        params = parse_qs(parsed.query)
        if "request_token" in params:
            request_token = params["request_token"][0]

    try:
        kite = KiteConnect(api_key=api_key)
        session_data = kite.generate_session(request_token=request_token, api_secret=api_secret)

        access_token = session_data["access_token"]
        public_token = session_data.get("public_token", "")
        user_name = session_data.get("user_name", "Trader")
        user_id = session_data.get("user_id", "")

        # Save to session_token.json on backend
        payload = {
            "api_key": api_key,
            "api_secret": api_secret,
            "user_id": user_id,
            "user_name": user_name,
            "access_token": access_token,
            "public_token": public_token,
            "login_time": datetime.now().isoformat(),
        }

        with open(TOKEN_FILE, "w") as f:
            json.dump(payload, f, indent=2)

        # Update cached client
        _cached_kite = kite

        # Update .env if exists
        try:
            if ENV_FILE.exists():
                set_key(str(ENV_FILE), "KITE_API_KEY", api_key)
                set_key(str(ENV_FILE), "KITE_API_SECRET", api_secret)
                set_key(str(ENV_FILE), "KITE_USER_ID", user_id)
        except Exception as e:
            logger.warning(f"Could not update .env: {e}")

        logger.info(f"User {user_id} ({user_name}) successfully authenticated.")

        # Sanitized response (NO access token sent to frontend)
        return {
            "success": True,
            "message": "Authentication successful",
            "user": {
                "user_id": user_id,
                "user_name": user_name,
                "login_time": payload["login_time"],
                "api_key": api_key[:4] + "****",
            },
        }

    except Exception as e:
        logger.error(f"Login failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Authentication failed: {str(e)}",
        )


@app.get("/api/profile")
def get_user_profile():
    """Fetches user profile details: User Name, User ID, Products, Exchanges, Email, Broker, etc."""
    kite = get_active_kite()
    if not kite:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated or session expired. Please log in.",
        )

    try:
        profile = kite.profile()
        return {
            "user_name": profile.get("user_name", "N/A"),
            "user_id": profile.get("user_id", "N/A"),
            "email": profile.get("email", "N/A"),
            "broker": profile.get("broker", "ZERODHA"),
            "user_type": profile.get("user_type", "individual"),
            "products": profile.get("products", ["CNC", "NRML", "MIS", "BO", "CO"]),
            "exchanges": profile.get("exchanges", ["NSE", "BSE", "NFO", "BFO", "CDS", "MCX"]),
            "order_types": profile.get("order_types", ["MARKET", "LIMIT", "SL", "SL-M"]),
            "avatar_url": profile.get("avatar_url", None),
        }
    except Exception as e:
        logger.error(f"Error fetching profile: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not fetch profile from Zerodha: {str(e)}",
        )


@app.get("/api/margins")
def get_user_margins():
    """Fetches equity and commodity account margins."""
    kite = get_active_kite()
    if not kite:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
        )

    try:
        margins = kite.margins()
        return margins
    except Exception as e:
        logger.error(f"Error fetching margins: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not fetch margins: {str(e)}",
        )


@app.post("/api/logout")
def logout():
    """Clears saved session on backend."""
    global _cached_kite
    _cached_kite = None
    if TOKEN_FILE.exists():
        try:
            TOKEN_FILE.unlink()
        except Exception as e:
            logger.warning(f"Error removing token file: {e}")

    return {"success": True, "message": "Logged out successfully"}


# =========================================================================
# Strategy & Backtesting API Endpoints
# =========================================================================

@app.get("/api/strategy/state")
def get_strategy_state():
    """Returns the configuration, state, and safety parameters of the 30-min ORB strategy."""
    from config.settings import settings
    from data.market_calendar import MarketCalendar
    from datetime import datetime

    now = datetime.now()
    phase = MarketCalendar.get_session_phase(now.time()).value

    return {
        "strategy_name": "30-Minute Volatility-Filtered Opening Range Breakout (ORB)",
        "symbol": settings.instruments[0].symbol,
        "exchange": settings.instruments[0].exchange,
        "instrument_type": settings.instruments[0].instrument_type.value,
        "lot_size": settings.instruments[0].lot_size,
        "session_phase": phase,
        "opening_range_window": "09:15 – 09:45 IST",
        "entry_window": "09:45 – 13:30 IST",
        "square_off_time": "14:30 IST",
        "hard_cutoff_time": "15:10 IST",
        "min_orb_range": settings.instruments[0].min_orb_range,
        "max_risk_cap": settings.instruments[0].max_risk_cap,
        "risk_reward_ratio": settings.strategy.risk_reward_ratio,
        "breakeven_r_multiple": settings.strategy.breakeven_r_multiple,
        "risk_per_trade_pct": settings.risk.risk_per_trade_pct * 100.0,
        "max_daily_loss_pct": settings.risk.max_daily_loss_pct * 100.0,
        "active_broker": settings.active_broker.value,
        "is_paper_trading": settings.active_broker.value == "PAPER",
    }


@app.get("/api/strategy/trades")
def get_strategy_trades():
    """Fetches all executed trades from SQLite journal."""
    from database.db import DatabaseManager
    from config.settings import settings

    db = DatabaseManager(settings.db_path)
    trades = db.get_all_trades()
    return {"trades": trades, "count": len(trades)}


@app.post("/api/strategy/backtest")
def trigger_backtest(days: int = 180):
    """Executes the event-driven backtester over historical 15m candles."""
    from backtest.event_engine import EventDrivenBacktester
    from config.settings import settings
    from data.historical_loader import HistoricalDataLoader
    from datetime import datetime

    df = HistoricalDataLoader.generate_synthetic_nifty_data(
        start_date=datetime(2025, 1, 1),
        days=days,
        base_price=24000.0,
    )
    inst = settings.instruments[0]
    backtester = EventDrivenBacktester(instrument=inst, app_settings=settings)
    report = backtester.run(df, initial_capital=settings.risk.initial_capital)

    return {
        "success": True,
        "report": {
            "total_trades": report.total_trades,
            "long_trades": report.long_trades,
            "short_trades": report.short_trades,
            "winning_trades": report.winning_trades,
            "losing_trades": report.losing_trades,
            "win_rate_pct": report.win_rate_pct,
            "gross_pnl": report.gross_pnl,
            "total_transaction_costs": report.total_transaction_costs,
            "net_pnl": report.net_pnl,
            "profit_factor": report.profit_factor,
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
        }
    }

