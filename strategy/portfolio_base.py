"""
Portfolio-Level Strategy Interface.

Why this exists instead of reusing BaseStrategy
-----------------------------------------------
strategy/base_strategy.py models ONE instrument, ONE open position, driven
bar-by-bar and tick-by-tick inside a single intraday session (ORB, CPR,
Dual-EMA all fit that shape). The two engines added here do not:

  * NSE-RM-100 holds up to 10 CNC equity positions simultaneously, across a
    ~100-symbol cross-section, rebalanced once a fortnight, held overnight
    for 10-50 sessions. A "signal" is a whole target-weight vector, not a
    BUY on one symbol.
  * NSE-VRP-INDEX opens a 4-leg Iron Condor as one atomic structure; its
    P&L, stop and target are properties of the structure, not of any leg.

Forcing either through on_candle(candle, vwap) -> Optional[StrategySignal]
would mean lying about the data model. They get their own ABC. The existing
three strategies are untouched.

Both engines are strictly non-look-ahead: every parameter is estimated from
data through session t-1, and day t data is used only to execute.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class MarketRegime(str, Enum):
    BULLISH = "BULLISH"
    DEFENSIVE = "DEFENSIVE"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class TargetPosition:
    """Desired end-state for one symbol after a rebalance."""
    symbol: str
    weight: float                 # fraction of total strategy capital, 0..1
    score: float                  # cross-sectional z-score that earned the slot
    rank: int                     # 1 = strongest in the active cross-section
    reference_price: float        # close of t-1, used for qty sizing
    quantity: int = 0             # filled in by size_targets()
    stop_loss: Optional[float] = None
    sector: Optional[str] = None


@dataclass
class RebalanceOrder:
    """One executable delta between current holdings and the target book."""
    symbol: str
    side: OrderSide
    quantity: int
    reference_price: float
    reason: str
    product: str = "CNC"
    order_type: str = "LIMIT"
    exchange: str = "NSE"


@dataclass
class HedgeOrder:
    """Index-futures macro hedge leg (cash-settled, no physical delivery)."""
    symbol: str
    side: OrderSide
    lots: int
    lot_size: int
    reference_price: float
    portfolio_beta: float
    notional: float
    reason: str = "DEFENSIVE_REGIME_BETA_HEDGE"
    product: str = "NRML"


@dataclass
class OptionLeg:
    """One leg of a defined-risk options structure."""
    tradingsymbol: str
    strike: float
    option_type: str              # "CE" or "PE"
    side: OrderSide
    lots: int
    lot_size: int
    premium: float                # per-unit price used for entry accounting
    delta: float
    expiry: date

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size

    @property
    def signed_premium(self) -> float:
        """+ve = credit received, -ve = debit paid, in rupees."""
        sign = 1.0 if self.side == OrderSide.SELL else -1.0
        return sign * self.premium * self.quantity


@dataclass
class StructureOrder:
    """A multi-leg options position treated as one atomic unit."""
    structure_id: str
    structure_type: str
    legs: List[OptionLeg]
    timestamp: datetime
    net_credit: float             # rupees, per structure (all lots)
    max_loss: float               # rupees, defined risk
    profit_target: float          # exit when unrealised profit >= this
    stop_loss: float              # exit when unrealised loss >= this
    expiry: date
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RebalancePlan:
    """Full output of one scheduled rebalance evaluation."""
    as_of: date
    regime: MarketRegime
    gross_exposure: float
    targets: List[TargetPosition]
    orders: List[RebalanceOrder]
    hedge: Optional[HedgeOrder] = None
    skipped: bool = False
    skip_reason: str = ""
    diagnostics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExitSignal:
    """Intra-cycle liquidation instruction for an open holding or structure."""
    symbol: str
    quantity: int
    price: float
    reason: str
    timestamp: datetime
    product: str = "CNC"


class PortfolioStrategy(ABC):
    """Base for EOD, multi-position and multi-leg engines."""

    name: str = "PORTFOLIO_STRATEGY"
    strategy_code: str = "GENERIC"

    @abstractmethod
    def is_rebalance_day(self, session_date: date) -> bool:
        """True when the scheduled evaluation cadence fires on this date."""

    @abstractmethod
    def generate_plan(self, *args: Any, **kwargs: Any) -> Any:
        """Produce the target book / structure for this evaluation date."""

    @abstractmethod
    def monitor(self, *args: Any, **kwargs: Any) -> List[ExitSignal]:
        """Daily/intraday check of open exposure for stop and trail breaches."""
