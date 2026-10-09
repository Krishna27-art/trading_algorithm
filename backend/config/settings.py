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

    # Hedge
    nifty_lot_size: int = DEFAULT_NIFTY_LOT_SIZE

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
    lot_size: int = DEFAULT_NIFTY_LOT_SIZE  # NIFTY options lot size -- current NSE contract spec

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


class CRSDConfig(BaseModel):
    # ---- factor model / training window
    horizon_bars: int = 3                      # h: residual accumulation window
    train_days: int = 20
    min_train_bars: int = 250
    min_coverage: float = 0.90                 # peer must cover >= this share of own bars
    group_cap: int = 8                         # max peers loaded into the factor model
    halflife_grid_days: Tuple[float, ...] = (3.0, 5.0, 8.0, 12.0)
    ridge_lambda: float = 1e-8
    transient_halflife_bars: float = 3.0
    use_local_beta: bool = True
    local_beta_window: int = 4
    local_beta_clip: Tuple[float, float] = (0.6, 1.5)

    # ---- basket construction
    max_peers: int = 4
    min_peers: int = 2
    min_peer_corr: float = 0.10
    hedge_ratio_clip: Tuple[float, float] = (0.5, 1.5)

    # ---- entry / exit thresholds (research grid: entry 2.0-3.5, exit 0.5-1.0)
    entry_z: float = 2.5
    exit_z: float = 0.75
    confirm_bars: int = 1
    cs_min_group: int = 4                      # cross-sectional z needs >= this many names
    cs_confirm_z: float = 1.0
    auto_calibrate: bool = False               # grid-search entry/exit inside training only
    entry_z_grid: Tuple[float, ...] = (2.0, 2.5, 3.0, 3.5)
    exit_z_grid: Tuple[float, ...] = (0.5, 0.75, 1.0)
    calib_min_events: int = 12
    require_convergence_evidence: bool = True
    min_hit_rate: float = 0.52
    min_net_bps: float = 0.0

    # ---- regime gate (BOCPD)
    bocpd_hazard: float = 1.0 / 60.0
    bocpd_max_run: int = 120
    bocpd_recent_window: int = 3
    bocpd_warm_bars: int = 300
    bocpd_kappa0: float = 0.1
    bocpd_alpha0: float = 2.0
    bocpd_beta0: float = 1.0
    cp_entry_max: float = 0.35
    cp_exit: float = 0.60

    # ---- volatility memory
    vm_exponent: float = 0.75
    vm_len: int = 120
    vm_stress_pctl: float = 0.90               # entries blocked at/above this
    vm_scale_pctl: float = 0.75                # scale halves at/above this

    # ---- liquidity
    liq_turnover_pctl: float = 0.30            # own/hedge turnover must exceed this pctl
    liq_spread_pctl: float = 0.90              # spread proxy must be below this pctl
    liq_shock_frac: float = 0.25               # turnover < frac*median for 2 bars -> shock

    # ---- costs
    round_trip_cost_bps_per_leg: float = 8.0   # brokerage+STT+charges+slippage
    edge_cost_mult: float = 2.0
    capture_frac: float = 0.60                 # share of the excess spread expected to convert

    # ---- exits
    max_hold_bars: int = 8
    stop_sigma: float = 2.0
    tail_quantile: float = 0.99
    leg_stop_mult: float = 2.0                 # price stop on the unhedged leg = mult x spread stop
    min_target_bps: float = 3.0
    max_trades_per_day: int = 2

    # ---- session
    entry_start: time = time(9, 45)
    entry_end: time = time(14, 15)
    session_end: time = time(15, 10)
    skip_expiry_day: bool = True
    expiry_weekday: int = 1                    # monthly expiry weekday (Tue)


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

    # Active Strategy (Options: "cpr", "dual_ema", "orb", "apex", "sector_impulse", "ssf_l5_srm", "aou_oss", "crsd", "rm100", "vrp")
    active_strategy: str = "cpr"

    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    initial_capital: float = 1000000.0  # ₹10,00,000 (10 Lakhs) reference capital for backtesting
    liquidity_filter: LiquidityFilterConfig = Field(default_factory=LiquidityFilterConfig)
    costs: TransactionCostConfig = Field(default_factory=TransactionCostConfig)

    # Portfolio-level strategies and cost schedules
    rm100: ResidualMomentumConfig = Field(default_factory=ResidualMomentumConfig)
    vrp: VRPConfig = Field(default_factory=VRPConfig)
    crsd: CRSDConfig = Field(default_factory=CRSDConfig)
    delivery_costs: DeliveryCostConfig = Field(default_factory=DeliveryCostConfig)
    options_costs: IndexOptionsCostConfig = Field(default_factory=IndexOptionsCostConfig)

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

