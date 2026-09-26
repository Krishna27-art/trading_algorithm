"""
Unified Broker Position Service.
Normalizes, deduplicates, and validates broker net positions for live and paper execution.
"""

from dataclasses import dataclass, field
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
    unrealised_pnl: float = 0.0
    realised_pnl: float = 0.0
    pnl: float = 0.0
    status: str = "OPEN"
    is_quarantined: bool = False
    quarantine_reason: Optional[str] = None
    strategy_stop_loss: Optional[float] = None
    strategy_target: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    trade_id: Optional[str] = None
    buy_quantity: int = 0
    sell_quantity: int = 0
    buy_price: float = 0.0
    sell_price: float = 0.0
    multiplier: int = 1
    m2m: float = 0.0
    day_buy_quantity: int = 0
    day_sell_quantity: int = 0
    day_buy_price: float = 0.0
    day_sell_price: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "position_id": self.position_id,
            "tradingsymbol": self.tradingsymbol,
            "exchange": self.exchange,
            "product": self.product,
            "quantity": self.quantity,
            "average_price": self.average_price,
            "last_price": self.last_price,
            "unrealised_pnl": self.unrealised_pnl,
            "realised_pnl": self.realised_pnl,
            "pnl": self.pnl,
            "status": self.status,
            "is_quarantined": self.is_quarantined,
            "quarantine_reason": self.quarantine_reason,
            "strategy_stop_loss": self.strategy_stop_loss,
            "strategy_target": self.strategy_target,
            "stop_loss": self.stop_loss or self.strategy_stop_loss,
            "target": self.target or self.strategy_target,
            "trade_id": self.trade_id,
            "buy_quantity": self.buy_quantity,
            "sell_quantity": self.sell_quantity,
            "buy_price": self.buy_price,
            "sell_price": self.sell_price,
            "multiplier": self.multiplier,
            "m2m": self.m2m,
        }


class PositionService:
    def __init__(self, kite_client: Optional[Any] = None):
        self.kite = kite_client

    def calculate_pnl(self, quantity: int, average_price: float, last_price: float) -> Tuple[float, float]:
        """Calculates unrealised and total P&L for long and short positions."""
        if quantity == 0:
            return 0.0, 0.0
        elif quantity > 0:
            pnl = (last_price - average_price) * quantity
        else:
            pnl = (average_price - last_price) * abs(quantity)
        return pnl, pnl

    def validate_position_data(
        self,
        tradingsymbol: str,
        exchange: str,
        quantity: int,
        average_price: float,
        last_price: float,
        lot_size: Optional[int] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Validates position payload against exchange lot rules."""
        if exchange in ("NFO", "BFO", "CDS", "MCX") and lot_size and lot_size > 0:
            if abs(quantity) % lot_size != 0:
                return (
                    False,
                    f"Quantity {quantity} is not a multiple of lot size {lot_size} for {tradingsymbol}",
                )
        return True, None

    def normalize_positions(
        self,
        raw_positions: List[Dict[str, Any]],
        strategy_positions: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[NormalizedPosition]:
        """
        Normalizes raw broker/simulator positions array.
        Deduplicates by (exchange, tradingsymbol, product).
        Applies LTP quarantine rules and matches strategy SL/Target parameters.
        """
        dedup_map: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for r in raw_positions:
            key = (
                r.get("exchange", "NSE"),
                r.get("tradingsymbol", ""),
                r.get("product", "MIS"),
            )
            dedup_map[key] = r

        result: List[NormalizedPosition] = []
        strategy_meta = strategy_positions or {}

        for (exchange, symbol, product), r in dedup_map.items():
            qty = int(r.get("quantity", 0))
            avg_price = float(r.get("average_price", 0.0) or 0.0)
            ltp = float(r.get("last_price", 0.0) or 0.0)
            raw_realised = float(r.get("realised", 0.0) or r.get("realised_pnl", 0.0) or 0.0)

            is_quarantined = False
            quarantine_reason = None

            if ltp == 0.0:
                is_quarantined = True
                quarantine_reason = "Impossible or zero LTP reported"
            elif avg_price > 0 and ltp > 0:
                if ltp < (avg_price * 0.2) or ltp > (avg_price * 5.0):
                    is_quarantined = True
                    quarantine_reason = "Impossible LTP divergence"

            if qty == 0:
                unrealised_pnl = 0.0
                realised_pnl = raw_realised
                status = "CLOSED"
            else:
                unrealised_pnl, _ = self.calculate_pnl(qty, avg_price, ltp)
                realised_pnl = raw_realised
                status = "OPEN"

            pnl = unrealised_pnl + realised_pnl

            meta = strategy_meta.get(symbol, {})
            sl = meta.get("stop_loss")
            tgt = meta.get("target")
            tid = meta.get("trade_id")

            pos_id = f"{exchange}:{symbol}:{product}"
            norm = NormalizedPosition(
                position_id=pos_id,
                tradingsymbol=symbol,
                exchange=exchange,
                product=product,
                quantity=qty,
                average_price=avg_price,
                last_price=ltp,
                unrealised_pnl=unrealised_pnl,
                realised_pnl=realised_pnl,
                pnl=pnl,
                status=status,
                is_quarantined=is_quarantined,
                quarantine_reason=quarantine_reason,
                strategy_stop_loss=sl,
                strategy_target=tgt,
                stop_loss=sl,
                target=tgt,
                trade_id=tid,
                buy_quantity=int(r.get("buy_quantity", 0)),
                sell_quantity=int(r.get("sell_quantity", 0)),
                buy_price=float(r.get("buy_price", 0.0) or 0.0),
                sell_price=float(r.get("sell_price", 0.0) or 0.0),
                multiplier=int(r.get("multiplier", 1)),
                m2m=float(r.get("m2m", 0.0) or 0.0),
                day_buy_quantity=int(r.get("day_buy_quantity", 0)),
                day_sell_quantity=int(r.get("day_sell_quantity", 0)),
                day_buy_price=float(r.get("day_buy_price", 0.0) or 0.0),
                day_sell_price=float(r.get("day_sell_price", 0.0) or 0.0),
            )
            result.append(norm)

        return result
