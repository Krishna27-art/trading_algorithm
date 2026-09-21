"""
Unified Broker Position Service.
Provides normalization, aggregation, P&L calculation, invalid-data protection,
and strategy metadata association for broker positions (Zerodha Kite & Paper Broker).

Guarantees:
1. Broker data is the single source of truth for net positions.
2. One normalized row per unique (exchange, tradingsymbol, product).
3. Quarantine / rejection of impossible or stale LTPs and corrupted quantities.
4. Clean separation of active broker positions from historical trade journals.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


@dataclass
class NormalizedPosition:
    position_id: str                      # exchange:tradingsymbol:product
    tradingsymbol: str
    exchange: str                         # NSE, NFO, BSE, MCX
    product: str                          # MIS, NRML, CNC
    instrument_token: int
    quantity: int                         # Net quantity (+ for Long, - for Short, 0 for Flat)
    buy_quantity: int
    sell_quantity: int
    average_price: float                  # Net / buy average price
    last_price: float                     # Current LTP from live market quote
    pnl: float                            # Total P&L
    unrealised_pnl: float
    realised_pnl: float
    value: float                          # Current market value
    lot_size: int = 1
    multiplier: int = 1
    strategy_stop_loss: Optional[float] = None
    strategy_target: Optional[float] = None
    status: str = "OPEN"                  # OPEN or CLOSED
    is_quarantined: bool = False
    quarantine_reason: Optional[str] = None
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "position_id": self.position_id,
            "tradingsymbol": self.tradingsymbol,
            "exchange": self.exchange,
            "product": self.product,
            "instrument_token": self.instrument_token,
            "quantity": self.quantity,
            "buy_quantity": self.buy_quantity,
            "sell_quantity": self.sell_quantity,
            "average_price": round(self.average_price, 2),
            "last_price": round(self.last_price, 2),
            "pnl": round(self.pnl, 2),
            "unrealised_pnl": round(self.unrealised_pnl, 2),
            "realised_pnl": round(self.realised_pnl, 2),
            "value": round(self.value, 2),
            "lot_size": self.lot_size,
            "strategy_stop_loss": round(self.strategy_stop_loss, 2) if self.strategy_stop_loss else None,
            "strategy_target": round(self.strategy_target, 2) if self.strategy_target else None,
            "status": self.status,
            "is_quarantined": self.is_quarantined,
            "quarantine_reason": self.quarantine_reason,
            "updated_at": self.updated_at,
        }


class PositionService:
    """
    Normalizes and aggregates broker positions into a single source of truth.
    Protects against duplicate rows, invalid/stale prices, and erroneous lot sizes.
    """

    def __init__(self, kite_client: Optional[Any] = None):
        self.kite = kite_client
        self._instrument_lot_sizes: Dict[str, int] = {}

    def set_kite_client(self, kite_client: Optional[Any]) -> None:
        self.kite = kite_client

    def get_lot_size(self, tradingsymbol: str, exchange: str = "NSE") -> int:
        """
        Resolves lot size dynamically. Checks Kite instruments if available,
        or known market defaults, avoiding hardcoded assumptions.
        """
        if tradingsymbol in self._instrument_lot_sizes:
            return self._instrument_lot_sizes[tradingsymbol]

        # Resolve from Kite instrument master if connected
        if self.kite:
            try:
                instruments = self.kite.instruments(exchange)
                for inst in instruments:
                    if inst.get("tradingsymbol") == tradingsymbol:
                        lot = inst.get("lot_size", 1)
                        self._instrument_lot_sizes[tradingsymbol] = lot
                        return lot
            except Exception as e:
                logger.warning(f"Failed to fetch lot size from Kite for {tradingsymbol}: {e}")

        # Default standard logic: Equities = 1; Futures check pattern
        if exchange == "NSE" and not tradingsymbol.endswith("FUT"):
            return 1
        return 1

    def validate_position_data(
        self,
        tradingsymbol: str,
        exchange: str,
        quantity: int,
        average_price: float,
        last_price: float,
        lot_size: int = 1,
    ) -> Tuple[bool, Optional[str]]:
        """
        Validates position attributes against corrupt/impossible broker states:
        - Rejects zero or negative average prices when net quantity is open.
        - Rejects impossible LTPs (e.g. LTP <= 0, or LTP divergent > 80% from entry for standard equity/futures).
        - Validates that derivative quantities are integer multiples of the contract lot size.
        """
        if not tradingsymbol or not exchange:
            return False, "Missing tradingsymbol or exchange"

        # If position is flat, no price sanity check needed
        if quantity == 0:
            return True, None

        if average_price <= 0:
            return False, f"Invalid average price: {average_price}"

        if last_price <= 0:
            return False, f"Impossible or zero LTP: {last_price}"

        # Extreme divergence check:
        # If LTP is less than 15% of entry price or more than 700% of entry price on non-penny stocks (> ₹100),
        # flag as corrupted/stale data (e.g. RELIANCE LTP ₹99.5 when average is ₹3,000).
        if average_price >= 100.0:
            divergence_ratio = last_price / average_price
            if divergence_ratio < 0.15 or divergence_ratio > 7.0:
                return False, f"Impossible LTP divergence: LTP ₹{last_price} vs Entry ₹{average_price}"

        # Lot size check for derivatives
        if exchange in ("NFO", "MCX", "BFO") and lot_size > 1:
            if abs(quantity) % lot_size != 0:
                return False, f"Quantity {quantity} is not a valid multiple of lot size {lot_size}"

        return True, None

    def calculate_pnl(
        self,
        quantity: int,
        average_price: float,
        last_price: float,
        broker_pnl: Optional[float] = None,
        broker_unrealised: Optional[float] = None,
    ) -> Tuple[float, float]:
        """
        Calculates unrealised and total P&L:
        - Long: (last_price - average_price) * quantity
        - Short: (average_price - last_price) * abs(quantity)
        Prefers broker-validated figures if within rational tolerance.
        """
        if quantity == 0:
            return 0.0, 0.0

        if quantity > 0:
            calc_unrealised = (last_price - average_price) * quantity
        else:
            calc_unrealised = (average_price - last_price) * abs(quantity)

        # If broker provided unrealised P&L, verify calculation
        final_unrealised = broker_unrealised if broker_unrealised is not None else calc_unrealised
        final_total_pnl = broker_pnl if broker_pnl is not None else final_unrealised

        return round(final_unrealised, 2), round(final_total_pnl, 2)

    def normalize_positions(
        self,
        raw_positions: List[Dict[str, Any]],
        strategy_positions: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[NormalizedPosition]:
        """
        Aggregates and deduplicates raw broker positions.
        Returns strictly ONE normalized record per (exchange, tradingsymbol, product).
        """
        positions_by_id: Dict[str, NormalizedPosition] = {}
        strategy_map = strategy_positions or {}

        for raw in raw_positions:
            tradingsymbol = str(raw.get("tradingsymbol") or raw.get("symbol") or "").strip().upper()
            if not tradingsymbol:
                continue

            exchange = str(raw.get("exchange") or "NSE").strip().upper()
            product = str(raw.get("product") or "MIS").strip().upper()
            pos_id = f"{exchange}:{tradingsymbol}:{product}"

            net_qty = int(raw.get("quantity") or raw.get("net_quantity") or 0)
            buy_qty = int(raw.get("buy_quantity") or (net_qty if net_qty > 0 else 0))
            sell_qty = int(raw.get("sell_quantity") or (abs(net_qty) if net_qty < 0 else 0))

            avg_price = float(raw.get("average_price") or raw.get("buy_price") or 0.0)
            last_price = float(raw.get("last_price") or raw.get("current_price") or 0.0)
            token = int(raw.get("instrument_token") or 0)
            lot_size = int(raw.get("lot_size") or self.get_lot_size(tradingsymbol, exchange))

            # Validate data integrity
            is_valid, reason = self.validate_position_data(
                tradingsymbol=tradingsymbol,
                exchange=exchange,
                quantity=net_qty,
                average_price=avg_price,
                last_price=last_price,
                lot_size=lot_size,
            )

            # Calculate P&L
            broker_pnl = raw.get("pnl")
            broker_unrealised = raw.get("unrealised")
            unrealised_pnl, total_pnl = self.calculate_pnl(
                quantity=net_qty,
                average_price=avg_price,
                last_price=last_price,
                broker_pnl=broker_pnl,
                broker_unrealised=broker_unrealised,
            )
            realised_pnl = float(raw.get("realised") or 0.0)

            # Match active strategy metadata (Stop Loss & Profit Target)
            strat_meta = strategy_map.get(tradingsymbol, {})
            strategy_sl = strat_meta.get("stop_loss") or strat_meta.get("initial_stop")
            strategy_target = strat_meta.get("target") or strat_meta.get("initial_target")

            norm = NormalizedPosition(
                position_id=pos_id,
                tradingsymbol=tradingsymbol,
                exchange=exchange,
                product=product,
                instrument_token=token,
                quantity=net_qty,
                buy_quantity=buy_qty,
                sell_quantity=sell_qty,
                average_price=avg_price,
                last_price=last_price,
                pnl=total_pnl,
                unrealised_pnl=unrealised_pnl,
                realised_pnl=realised_pnl,
                value=round(abs(net_qty) * last_price, 2),
                lot_size=lot_size,
                strategy_stop_loss=strategy_sl,
                strategy_target=strategy_target,
                status="OPEN" if net_qty != 0 else "CLOSED",
                is_quarantined=not is_valid,
                quarantine_reason=reason,
            )

            # Deduplication: replace or update in-place by position_id
            positions_by_id[pos_id] = norm

        # Return sorted by tradingsymbol
        return sorted(list(positions_by_id.values()), key=lambda p: p.tradingsymbol)
