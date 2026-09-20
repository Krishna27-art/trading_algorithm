"""
Portfolio State Management: Capital, Margins, Day P&L, and Position Tracking.
"""

from typing import Dict, Optional
from config.settings import RiskConfig, settings
from monitoring.logger import logger


class PortfolioManager:
    def __init__(self, initial_capital: float = settings.risk.initial_capital):
        self.initial_capital: float = initial_capital
        self.current_capital: float = initial_capital
        self.realized_pnl_today: float = 0.0
        self.unrealized_pnl_today: float = 0.0
        self.active_positions: Dict[str, dict] = {}

    def reset_day(self):
        """Resets daily P&L tracking while maintaining current capital balance."""
        self.realized_pnl_today = 0.0
        self.unrealized_pnl_today = 0.0
        self.active_positions.clear()

    def update_unrealized_pnl(self, symbol: str, current_price: float):
        if symbol in self.active_positions:
            pos = self.active_positions[symbol]
            qty = pos["quantity"]
            entry = pos["entry_price"]
            if pos["direction"] == "BUY":
                pos["unrealized_pnl"] = (current_price - entry) * qty
            else:
                pos["unrealized_pnl"] = (entry - current_price) * qty
            self.unrealized_pnl_today = sum(p.get("unrealized_pnl", 0.0) for p in self.active_positions.values())

    def record_entry(self, symbol: str, direction: str, quantity: int, price: float):
        self.active_positions[symbol] = {
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
            "entry_price": price,
            "unrealized_pnl": 0.0,
        }
        logger.info(f"Portfolio updated: Added {direction} {quantity} {symbol} @ ₹{price:.2f}")

    def record_exit(self, symbol: str, exit_price: float, net_pnl: float):
        if symbol in self.active_positions:
            del self.active_positions[symbol]
        self.realized_pnl_today += net_pnl
        self.current_capital += net_pnl
        self.unrealized_pnl_today = sum(p.get("unrealized_pnl", 0.0) for p in self.active_positions.values())
        logger.info(
            f"Portfolio updated: Closed {symbol} @ ₹{exit_price:.2f} | Net Trade P&L: ₹{net_pnl:,.2f} | "
            f"Total Capital: ₹{self.current_capital:,.2f}"
        )

    def get_summary(self) -> dict:
        total_pnl = self.realized_pnl_today + self.unrealized_pnl_today
        return {
            "starting_capital": self.initial_capital,
            "current_capital": self.current_capital,
            "realized_pnl": self.realized_pnl_today,
            "unrealized_pnl": self.unrealized_pnl_today,
            "total_day_pnl": total_pnl,
            "return_pct": (total_pnl / self.initial_capital) * 100.0,
            "open_positions": len(self.active_positions),
        }
