"""
Production Trading System Configuration
Centralized configuration management with validation via Pydantic.
"""

from datetime import time
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BrokerType(str, Enum):
    PAPER = "PAPER"
    KITE = "KITE"
    DHAN = "DHAN"


class InstrumentType(str, Enum):
    FUTURES = "FUTURES"
    EQUITY = "EQUITY"


class InstrumentConfig(BaseModel):
    symbol: str
    exchange: str = "NFO"
    instrument_type: InstrumentType = InstrumentType.FUTURES
    lot_size: int = 25  # Nifty default lot size
    tick_size: float = 0.05
    min_orb_range: float = 40.0
    max_orb_range: float = 120.0
    max_risk_cap: float = 80.0
    # Numeric Kite instrument_token for this contract — required by the
    # Historical Data API (a trading symbol string is not accepted there).
    # Resolve once with HistoricalDataLoader.resolve_instrument_token(...)
    # and paste the result here; it changes every futures expiry.
    instrument_token: Optional[int] = None


class StrategyConfig(BaseModel):
    # Strategy Schedule (IST)
    market_open: time = time(9, 15)
    orb_end: time = time(9, 45)
    entry_start: time = time(9, 45)
    entry_end: time = time(13, 30)
    square_off_time: time = time(14, 30)
    hard_cutoff_time: time = time(15, 10)

    # Signal & Risk parameters
    candle_timeframe_minutes: int = 15
    monitoring_timeframe_minutes: int = 5
    risk_reward_ratio: float = 2.0
    breakeven_r_multiple: float = 1.0
    max_trades_per_instrument_day: int = 1


class RiskConfig(BaseModel):
    initial_capital: float = 1000000.0  # ₹10,00,000 (10 Lakhs)
    risk_per_trade_pct: float = 0.01    # 1% per trade
    max_daily_loss_pct: float = 0.02    # 2% hard daily circuit breaker
    enforce_margin_check: bool = True
    allow_averaging: bool = False       # Never average down
    allow_overnight: bool = False       # Strictly intraday


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


class DeliveryCostConfig(BaseModel):
    brokerage_per_order: float = 0.0        # Zerodha CNC delivery is free
    stt_buy_pct: float = 0.0010             # 0.10% on buy turnover
    stt_sell_pct: float = 0.0010            # 0.10% on sell turnover
    exchange_txn_pct: float = 0.0000297     # 0.00297% on aggregate turnover
    sebi_charges_pct: float = 0.0000010     # Rs 10 per crore
    stamp_duty_buy_pct: float = 0.00015     # 0.015% on buy turnover
    gst_pct: float = 0.18
    slippage_pct: float = 0.0005            # 5 bps per execution leg
    dp_charges_per_sell_scrip: float = 15.34  # CDSL + broker, per scrip per day


class IndexOptionsCostConfig(BaseModel):
    brokerage_per_order: float = 20.0       # flat, per leg
    stt_sell_premium_pct: float = 0.0010    # 0.10% of premium, sell side only
    exchange_txn_premium_pct: float = 0.0003503   # 0.03503% of premium
    sebi_charges_pct: float = 0.0000010
    stamp_duty_buy_pct: float = 0.00003     # 0.003% on buy premium turnover
    gst_pct: float = 0.18
    slippage_premium_pct: float = 0.0075    # 0.75% of premium per leg


class ResidualMomentumConfig(BaseModel):
    # Estimation windows (trading days)
    regression_window: int = 252        # M: rolling OLS calibration
    momentum_window: int = 126          # K: residual accumulation lookback
    lag_buffer: int = 10                # L: recent sessions excluded
    vol_window: int = 20                # sizing volatility lookback
    adtv_window: int = 20

    # Universe / liquidity gates
    min_adtv_rupees: float = 50 * 1e7
    min_valid_universe: int = 70        # below this, skip the whole cycle

    # Selection
    portfolio_size: int = 10            # J
    top_decile_pct: float = 0.10
    exit_percentile: float = 0.75       # rescored holdings below P75 are cut

    # Sizing
    max_weight: float = 0.15
    min_weight: float = 0.04
    max_sector_weight: float = 0.30

    # Regime
    regime_sma: int = 200
    bullish_gross_exposure: float = 1.00
    defensive_gross_exposure: float = 0.40
    trend_filter_sma: int = 50

    # Exits
    stop_atr_multiple: float = 2.5
    atr_window: int = 14
    trail_trigger_gain: float = 0.15    # +15% unrealised arms the EMA20 trail
    trail_ema: int = 20

    # Risk gate
    max_drawdown_gate: float = 0.12     # -12% from high-water mark
    drawdown_exposure_cut: float = 0.50

    # Hedge
    nifty_lot_size: int = 25

    # Schedule
    entry_time: time = time(15, 0)
    order_deadline: time = time(15, 10)
    rebalance_weekday: int = 4          # Friday
    rebalance_parity_weeks: int = 2     # every alternate Friday

    # Slippage assumption used for reference pricing only
    slippage_pct: float = 0.0005


class VRPConfig(BaseModel):
    # Signal
    rv_ema_span: int = 20
    vrp_zscore_window: int = 60
    min_vrp_zscore: float = 0.50

    # Absolute regime band on India VIX
    vix_floor: float = 12.0
    vix_ceiling: float = 23.0

    # Structure deltas
    short_delta: float = 0.15
    long_delta: float = 0.05
    lots: int = 1
    lot_size: int = 75            # NIFTY options lot size -- verify each cycle

    # Exits
    profit_target_pct: float = 0.65     # of initial net credit
    stop_loss_multiple: float = 1.50    # of initial net credit
    expiry_exit_time: time = time(14, 30)

    # Schedule
    entry_weekday: int = 3              # Thursday
    entry_time: time = time(15, 10)

    # Costs / execution
    premium_slippage_pct: float = 0.0075   # 0.75% of premium per leg
    risk_free_rate: float = 0.065


class AppSettings(BaseSettings):
    # Active broker mode (DEFAULT: PAPER for safety)
    active_broker: BrokerType = BrokerType.PAPER
    
    # Target Instruments
    instruments: List[InstrumentConfig] = [
        InstrumentConfig(
            symbol="NIFTY",
            exchange="NFO",
            instrument_type=InstrumentType.FUTURES,
            lot_size=25,
            min_orb_range=40.0,
            max_orb_range=120.0,
            max_risk_cap=80.0,
        )
    ]

    # Active Strategy (Options: "cpr", "dual_ema", "orb", "rm100", "vrp")
    active_strategy: str = "cpr"

    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    costs: TransactionCostConfig = Field(default_factory=TransactionCostConfig)

    # Portfolio-level strategies and cost schedules
    rm100: ResidualMomentumConfig = Field(default_factory=ResidualMomentumConfig)
    vrp: VRPConfig = Field(default_factory=VRPConfig)
    delivery_costs: DeliveryCostConfig = Field(default_factory=DeliveryCostConfig)
    options_costs: IndexOptionsCostConfig = Field(default_factory=IndexOptionsCostConfig)

    # Storage paths
    base_dir: Path = Path(__file__).resolve().parent.parent
    db_path: Path = Path(__file__).resolve().parent.parent / "database" / "trading_system.db"
    log_dir: Path = Path(__file__).resolve().parent.parent / "logs"
    token_file: Path = Path(__file__).resolve().parent.parent / "session_token.json"

    # Broker API Keys (read from .env)
    kite_api_key: Optional[str] = None
    kite_api_secret: Optional[str] = None
    kite_access_token: Optional[str] = None
    kite_user_id: Optional[str] = None
    kite_totp_key: Optional[str] = None

    dhan_client_id: Optional[str] = None
    dhan_access_token: Optional[str] = None

    # Risk limits from .env (₹-denominated caps, separate from percentage-based kill-switch)
    max_capital_per_trade: float = 10000.0
    max_daily_loss_limit: float = 2000.0
    semi_automated_confirmation: bool = True

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

