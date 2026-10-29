"""
Production Trading System Configuration
Centralized configuration management with validation via Pydantic.
"""

from datetime import time
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Current NSE Index Derivative Contract Specifications (effective November 20, 2024 SEBI revision)
DEFAULT_NIFTY_LOT_SIZE: int = 75


class InstrumentType(str, Enum):
    FUTURES = "FUTURES"
    EQUITY = "EQUITY"
    INDEX = "INDEX"


class InstrumentConfig(BaseModel):
    symbol: str
    exchange: str = "NFO"
    instrument_type: InstrumentType = InstrumentType.FUTURES
    lot_size: int = DEFAULT_NIFTY_LOT_SIZE  # Current NSE NIFTY contract lot size
    tick_size: float = 0.05
    min_orb_range: float = 40.0
    max_orb_range: float = 120.0
    # Numeric Kite instrument_token for this contract — required by the
    # Historical Data API (a trading symbol string is not accepted there).
    # Resolve once with HistoricalDataLoader.resolve_instrument_token(...)
    # and paste the result here; it changes every futures expiry.
    instrument_token: Optional[int] = None

    # Equity-specific ORB controls.
    # None means the equity constraint is not configured.
    equity_orb_min_range_pct: Optional[float] = None
    equity_orb_max_range_pct: Optional[float] = None



class StrategyConfig(BaseModel):
    # Strategy Schedule (IST)
    market_open: time = time(9, 15)
    orb_end: time = time(9, 45)
    entry_start: time = time(9, 45)
    entry_end: time = time(13, 30)
    square_off_time: time = time(14, 30)
    hard_cutoff_time: time = time(15, 10)

    # Signal parameters
    candle_timeframe_minutes: int = 15
    monitoring_timeframe_minutes: int = 5
    risk_reward_ratio: float = 2.0
    breakeven_r_multiple: float = 1.0
    max_trades_per_instrument_day: int = 1


class LiquidityFilterConfig(BaseModel):
    """Configurable threshold parameters for the universe liquidity filter layer."""
    min_stock_price: float = 20.0           # Minimum stock price in INR
    min_avg_volume: int = 25000             # Minimum 20-day average volume
    min_avg_traded_value: float = 1000000.0 # Minimum average daily traded value (ADTV in INR, 10L)
    max_spread_pct: float = 1.5             # Maximum acceptable bid-ask spread %
    reject_circuits: bool = True            # Filter out upper/lower circuit locked stocks


class TransactionCostConfig(BaseModel):
    """Statutory cost schedule adhering to revised October 2024 SEBI & NSE norms."""
    # Futures costs
    futures_brokerage_per_order: float = 20.0
    futures_stt_sell_pct: float = 0.00020        # 0.02% on sell turnover
    futures_exchange_txn_pct: float = 0.0000190  # 0.00190%
    futures_stamp_duty_buy_pct: float = 0.000020 # 0.002% on buy
    futures_sebi_charges_pct: float = 0.0000010  # ₹10 per crore (0.0001%)
    futures_gst_pct: float = 0.18                # 18% on (brokerage + txn + sebi)
    futures_slippage_points: float = 0.50        # Conservative 0.50 index points

    # Equity MIS costs
    equity_brokerage_pct: float = 0.0003         # 0.03% or ₹20 (lower)
    equity_brokerage_cap: float = 20.0
    equity_stt_sell_pct: float = 0.00025         # 0.025% on sell turnover
    equity_exchange_txn_pct: float = 0.0000325   # 0.00325%
    equity_stamp_duty_buy_pct: float = 0.000030  # 0.003% on buy
    equity_sebi_charges_pct: float = 0.0000010   # ₹10 per crore
    equity_gst_pct: float = 0.18
    equity_slippage_pct: float = 0.0002          # 0.02% or 1 tick


class AppSettings(BaseSettings):
    # Target Instruments
    instruments: List[InstrumentConfig] = [
        InstrumentConfig(
            symbol="NIFTY",
            exchange="NFO",
            instrument_type=InstrumentType.FUTURES,
            lot_size=DEFAULT_NIFTY_LOT_SIZE,
            min_orb_range=40.0,
            max_orb_range=120.0,
        )
    ]

    # Default strategy shown when none is selected (Options: "gap_fade", "rsi_ema", "orb_sip")
    active_strategy: str = "gap_fade"

    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    initial_capital: float = 1000000.0  # ₹10,00,000 (10 Lakhs) reference capital for backtesting
    liquidity_filter: LiquidityFilterConfig = Field(default_factory=LiquidityFilterConfig)
    costs: TransactionCostConfig = Field(default_factory=TransactionCostConfig)

    # Storage paths
    base_dir: Path = Path(__file__).resolve().parent.parent.parent
    db_path: Path = Path(__file__).resolve().parent.parent / "database" / "trading_system.db"
    log_dir: Path = Path(__file__).resolve().parent.parent.parent / "logs"
    token_file: Path = Path(__file__).resolve().parent.parent.parent / "session_token.json"

    # Broker API Keys (read from .env)
    kite_api_key: Optional[str] = None
    kite_api_secret: Optional[str] = None
    kite_access_token: Optional[str] = None
    kite_user_id: Optional[str] = None
    kite_totp_key: Optional[str] = None

    # Security — NO DEFAULT: app refuses to start without this set in .env.
    # Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"
    app_shared_secret: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def validate_api_credentials(self) -> bool:
        """Verify that Kite API Key is set."""
        if not self.kite_api_key or self.kite_api_key == "your_api_key_here":
            print("[!] ERROR: KITE_API_KEY is not set in .env file.")
            return False
        return True


settings = AppSettings()

