"""
Unified Read-Only Broker Position Service & Normalizer.
Provides deduplication, quarantine checks for stale/divergent LTPs,
and strategy metadata enrichment for read-only portfolio monitoring.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class NormalizedPosition:
    position_id: str
    tradingsymbol: str
    exchange: str
    product: str
    quantity: int
    average_price: float
    last_price: float
    unrealised_pnl: float
    realised_pnl: float
    pnl: float
    strategy_stop_loss: Optional[float] = None
    strategy_target: Optional[float] = None
    trade_id: Optional[str] = None
    status: str = "OPEN"
    is_quarantined: bool = False
    quarantine_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PositionService:
    """Read-only broker position normalizer and validator."""

    def __init__(self, kite_client: Optional[Any] = None):
        self.kite = kite_client

    @staticmethod
    def calculate_pnl(quantity: int, average_price: float, last_price: float) -> Tuple[float, float]:
        """Calculates unrealised and total pnl for open position."""
        if quantity == 0:
            return 0.0, 0.0
        pnl = round((last_price - average_price) * quantity, 2)
        return pnl, pnl

    @staticmethod
    def validate_position_data(
        tradingsymbol: str,
        exchange: str,
        quantity: int,
        average_price: float,
        last_price: float,
        lot_size: int = 1,
    ) -> Tuple[bool, Optional[str]]:
        if quantity == 0:
            return True, None
        if lot_size > 1 and abs(quantity) % lot_size != 0:
            return False, f"Quantity {quantity} must be a multiple of lot size {lot_size}"
        if average_price <= 0:
            return False, "Average price must be greater than zero"
        return True, None

    def normalize_positions(
        self,
        raw_positions: List[Dict[str, Any]],
        strategy_positions: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[NormalizedPosition]:
        """
        Collapses raw broker positions into deduplicated NormalizedPosition objects.
        Validates prices, quarantees abnormal divergence, and attaches strategy stop/target.
        """
        strategy_positions = strategy_positions or {}
        pos_by_key: Dict[str, NormalizedPosition] = {}

        for raw in raw_positions:
            sym = raw.get("tradingsymbol", "")
            exch = raw.get("exchange", "NSE")
            prod = raw.get("product", "MIS")
            qty = int(raw.get("quantity", 0) or raw.get("net_quantity", 0) or 0)
            avg_p = float(raw.get("average_price", 0.0) or raw.get("buy_price", 0.0) or 0.0)
            ltp = float(raw.get("last_price", 0.0) or (avg_p if "last_price" not in raw else 0.0))
            realised = float(raw.get("realised", 0.0) or raw.get("realised_pnl", 0.0) or 0.0)

            key = f"{exch}:{sym}:{prod}"

            # Check for unreasonable / impossible LTP divergence or zero LTP
            is_quarantined = False
            quarantine_reason = None
            if ltp <= 0:
                is_quarantined = True
                quarantine_reason = "Missing or zero LTP received from broker"
            elif avg_p > 0 and ltp > 0:
                divergence = abs(ltp - avg_p) / avg_p
                if divergence > 0.80:
                    is_quarantined = True
                    quarantine_reason = f"Impossible LTP divergence ({divergence * 100:.1f}%) vs avg price {avg_p}"

            unrealised, _ = self.calculate_pnl(qty, avg_p, ltp)
            total_pnl = round(unrealised + realised, 2)

            strat_meta = strategy_positions.get(sym, {})
            sl = strat_meta.get("stop_loss")
            target = strat_meta.get("target")
            trade_id = strat_meta.get("trade_id")

            norm = NormalizedPosition(
                position_id=key,
                tradingsymbol=sym,
                exchange=exch,
                product=prod,
                quantity=qty,
                average_price=round(avg_p, 2),
                last_price=round(ltp, 2),
                unrealised_pnl=unrealised,
                realised_pnl=round(realised, 2),
                pnl=total_pnl,
                strategy_stop_loss=sl,
                strategy_target=target,
                trade_id=trade_id,
                status="OPEN" if qty != 0 else "CLOSED",
                is_quarantined=is_quarantined,
                quarantine_reason=quarantine_reason,
            )

            # Deduplicate by key (in-place update)
            pos_by_key[key] = norm

        return list(pos_by_key.values())

