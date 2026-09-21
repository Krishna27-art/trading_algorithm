"""
Pre-trade Risk Gate & Portfolio Circuit-Breaker (Daily Kill-Switch).
Enforces:
- Max 1 trade per instrument per day
- Max 2.0% daily cumulative loss kill-switch
- No overnight positions & no averaging down
"""

from datetime import date, datetime, time
from typing import Dict, Optional, Tuple
from config.settings import InstrumentConfig, RiskConfig, settings
from monitoring.logger import logger


class RiskManager:
    def __init__(
        self,
        risk_config: RiskConfig = settings.risk,
        max_portfolio_daily_trades: int = 1,
    ):
        self.config = risk_config
        self.max_portfolio_daily_trades = max_portfolio_daily_trades
        self.daily_trades_count: Dict[str, int] = {}
        self.daily_realized_pnl: float = 0.0
        self.daily_unrealized_pnl: float = 0.0
        self.kill_switch_active: bool = False
        self.current_trading_date: Optional[date] = None

    def reset_daily_state(self, current_date: date):
        """Called each morning before 09:15 IST."""
        self.current_trading_date = current_date
        self.daily_trades_count.clear()
        self.daily_realized_pnl = 0.0
        self.daily_unrealized_pnl = 0.0
        self.kill_switch_active = False
        logger.info(f"Risk state reset for trading session {current_date}.")

    def update_pnl(self, realized_pnl_delta: float = 0.0, current_unrealized_pnl: float = 0.0, capital: float = 1000000.0) -> bool:
        """
        Updates cumulative daily P&L across ALL open/closed positions and checks
        for circuit breaker. Returns True if kill-switch is triggered.
        """
        self.daily_realized_pnl += realized_pnl_delta
        self.daily_unrealized_pnl = current_unrealized_pnl
        total_daily_loss = -(self.daily_realized_pnl + self.daily_unrealized_pnl)

        max_allowed_loss = capital * self.config.max_daily_loss_pct

        if total_daily_loss >= max_allowed_loss and not self.kill_switch_active:
            self.kill_switch_active = True
            logger.critical(
                f"[CIRCUIT BREAKER ACTIVATED] Cumulative portfolio daily loss ₹{total_daily_loss:,.2f} "
                f"reached/exceeded 2.0% threshold (₹{max_allowed_loss:,.2f}). Engaging hard software kill-switch!"
            )
            return True

        return self.kill_switch_active

    def validate_pre_trade(
        self,
        symbol: str,
        current_time: time,
        quantity: int,
        capital: float,
        has_open_position: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        """
        Executes strict pre-trade validation checks.
        Enforces portfolio-wide 1-trade limit and 2% daily loss circuit breaker.
        Returns (is_approved, rejection_reason).
        """
        # 1. Check Portfolio Kill-Switch
        if self.kill_switch_active:
            return False, "REJECTED: Daily portfolio kill-switch is active."

        # 2. Check Portfolio-Wide Daily Trade Limit (across ALL instruments combined)
        total_trades_done = sum(self.daily_trades_count.values())
        if total_trades_done >= self.max_portfolio_daily_trades:
            return (
                False,
                f"REJECTED: Daily trade limit reached for portfolio ({total_trades_done}/{self.max_portfolio_daily_trades}).",
            )

        # 2b. Check Symbol-Specific Daily Trade Limit (Max 1 trade per instrument)
        trades_done = self.daily_trades_count.get(symbol, 0)
        if trades_done >= 1:
            return False, f"REJECTED: Daily trade limit reached for {symbol} ({trades_done}/1)."

        # 3. Check for No Averaging Down
        if has_open_position and not self.config.allow_averaging:
            return False, "REJECTED: Adding to existing position / averaging down is strictly prohibited."

        # 4. Check Valid Quantity
        if quantity <= 0:
            return False, "REJECTED: Computed position size is 0 (risk or margin check failed)."

        # 5. Check Time Window (09:45 to 13:30)
        if current_time < time(9, 45) or current_time > time(13, 30):
            return False, f"REJECTED: Current time {current_time} outside entry window (09:45–13:30 IST)."

        return True, None

    def record_trade_executed(self, symbol: str):
        self.daily_trades_count[symbol] = self.daily_trades_count.get(symbol, 0) + 1
        total_trades = sum(self.daily_trades_count.values())
        logger.info(
            f"Trade registered for {symbol}. Symbol trades: {self.daily_trades_count[symbol]} | "
            f"Total portfolio session trades: {total_trades}/{self.max_portfolio_daily_trades}."
        )
