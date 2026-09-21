"""
Database schema & data structures for trade journaling and audit logs.
"""

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"


class OrderDirection(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ExitReason(str, Enum):
    PROFIT_TARGET = "PROFIT_TARGET"
    STOP_LOSS = "STOP_LOSS"
    BREAKEVEN_SL = "BREAKEVEN_SL"
    TIME_SQUARE_OFF = "TIME_SQUARE_OFF"
    KILL_SWITCH = "KILL_SWITCH"
    MANUAL = "MANUAL"


class TradeRecord(BaseModel):
    id: Optional[int] = None
    trade_id: str
    symbol: str
    direction: OrderDirection
    entry_time: datetime
    entry_price: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    quantity: int
    initial_stop: float
    initial_target: float
    exit_reason: Optional[ExitReason] = None
    pnl_gross: float = 0.0
    pnl_net: float = 0.0
    total_costs: float = 0.0
    brokerage: float = 0.0
    stt: float = 0.0
    exchange_charges: float = 0.0
    gst: float = 0.0
    sebi_charges: float = 0.0
    stamp_duty: float = 0.0
    slippage: float = 0.0
    r_multiple: float = 0.0
    is_paper: bool = True
    notes: Optional[str] = None


class OrderRecord(BaseModel):
    order_id: str
    broker_order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    signal_id: Optional[str] = None
    symbol: str
    direction: OrderDirection
    order_type: OrderType
    price: Optional[float] = None
    quantity: int
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    average_fill_price: float = 0.0
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    reject_reason: Optional[str] = None
    tag: Optional[str] = None
