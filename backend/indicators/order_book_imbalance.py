from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class OrderBookFacts:
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    spread: Optional[float] = None
    spread_pct: Optional[float] = None
    bid_depth: int = 0
    ask_depth: int = 0
    imbalance: float = 0.0
    pressure: str = "NEUTRAL"
    top_bid_qty: int = 0
    top_ask_qty: int = 0


class OrderBookImbalanceEngine:
    def __init__(self, imbalance_threshold: float = 0.2):
        self.imbalance_threshold = imbalance_threshold

    def calculate_imbalance(self, depth_data: Any) -> OrderBookFacts:
        if not depth_data:
            return OrderBookFacts()

        bids: List[Tuple[float, int, int]] = []
        asks: List[Tuple[float, int, int]] = []

        if hasattr(depth_data, "bids") and hasattr(depth_data, "asks"):
            bids = getattr(depth_data, "bids") or []
            asks = getattr(depth_data, "asks") or []
        elif isinstance(depth_data, dict):
            raw_bids = depth_data.get("buy") or depth_data.get("bids") or []
            raw_asks = depth_data.get("sell") or depth_data.get("asks") or []

            for item in raw_bids:
                if isinstance(item, dict):
                    bids.append((float(item.get("price", 0)), int(item.get("quantity", 0)), int(item.get("orders", 1))))
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    bids.append((float(item[0]), int(item[1]), int(item[2]) if len(item) > 2 else 1))

            for item in raw_asks:
                if isinstance(item, dict):
                    asks.append((float(item.get("price", 0)), int(item.get("quantity", 0)), int(item.get("orders", 1))))
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    asks.append((float(item[0]), int(item[1]), int(item[2]) if len(item) > 2 else 1))

        if not bids and not asks:
            return OrderBookFacts()

        best_bid = float(bids[0][0]) if bids else None
        best_ask = float(asks[0][0]) if asks else None

        top_bid_qty = int(bids[0][1]) if bids else 0
        top_ask_qty = int(asks[0][1]) if asks else 0

        spread = None
        spread_pct = None
        if best_bid is not None and best_ask is not None and best_bid > 0:
            spread = round(best_ask - best_bid, 2)
            spread_pct = round((spread / best_bid) * 100.0, 3)

        total_bid_depth = sum(int(b[1]) for b in bids)
        total_ask_depth = sum(int(a[1]) for a in asks)

        total_depth = total_bid_depth + total_ask_depth
        imbalance = 0.0
        if total_depth > 0:
            imbalance = round((total_bid_depth - total_ask_depth) / total_depth, 3)

        pressure = "NEUTRAL"
        if imbalance >= self.imbalance_threshold:
            pressure = "BULLISH"
        elif imbalance <= -self.imbalance_threshold:
            pressure = "BEARISH"

        return OrderBookFacts(
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            spread_pct=spread_pct,
            bid_depth=total_bid_depth,
            ask_depth=total_ask_depth,
            imbalance=imbalance,
            pressure=pressure,
            top_bid_qty=top_bid_qty,
            top_ask_qty=top_ask_qty,
        )
