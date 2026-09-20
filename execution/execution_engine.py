"""
Production Execution Orchestrator.
Connects Market Data Ingestion -> Candle Aggregator -> Strategy -> Risk Gate -> Broker Gateway.
"""

import uuid
from datetime import datetime, time
from typing import Dict, List, Optional

from broker.base_broker import BaseBrokerAdapter
from config.settings import AppSettings, InstrumentConfig, settings
from data.candle_aggregator import CandleAggregator
from database.db import DatabaseManager
from database.models import ExitReason, OrderDirection, OrderRecord, OrderStatus, OrderType, TradeRecord
from execution.order_manager import OrderManager
from monitoring.logger import logger
from portfolio.portfolio_manager import PortfolioManager
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from risk.transaction_costs import TransactionCostCalculator
from strategy.base_strategy import SignalAction, StrategySignal
from strategy.orb_strategy import IntradayORBStrategy


class ExecutionEngine:
    def __init__(
        self,
        broker: BaseBrokerAdapter,
        instrument: InstrumentConfig,
        app_settings: AppSettings = settings,
        db: Optional[DatabaseManager] = None,
    ):
        self.broker = broker
        self.instrument = instrument
        self.settings = app_settings
        self.db = db or DatabaseManager(app_settings.db_path)

        # Core Components
        self.portfolio = PortfolioManager(initial_capital=app_settings.risk.initial_capital)
        self.risk_manager = RiskManager(app_settings.risk)
        self.position_sizer = PositionSizer(app_settings.risk)
        self.cost_calculator = TransactionCostCalculator(app_settings.costs)
        self.order_manager = OrderManager()

        # Strategy & Aggregator
        self.strategy = IntradayORBStrategy(instrument=self.instrument, strategy_config=app_settings.strategy)
        self.candle_aggregator = CandleAggregator(
            symbol=instrument.symbol,
            timeframe_minutes=app_settings.strategy.candle_timeframe_minutes,
            on_candle_close=self.on_candle_completed,
        )

        # Active trade state
        self.current_trade: Optional[TradeRecord] = None

    def start(self):
        """Initializes broker connection and begins session."""
        logger.info(f"Starting Execution Engine in [{self.broker.name}] mode for {self.instrument.symbol}.")
        self.broker.connect()
        self.risk_manager.reset_daily_state(datetime.now().date())
        self.strategy.reset_session(datetime.now().date())
        self.portfolio.reset_day()
        self.candle_aggregator.reset_daily_session()

    def stop(self):
        """Clean shutdown and square off any remaining positions."""
        logger.info("Stopping Execution Engine...")
        self.square_off_all_positions(reason=ExitReason.MANUAL)
        self.broker.disconnect()

    def process_tick(self, price: float, volume: int, timestamp: datetime):
        """Processes continuous real-time market data tick."""
        # 1. Update Portfolio Unrealized PnL
        self.portfolio.update_unrealized_pnl(self.instrument.symbol, price)

        # 2. Check Daily Max Loss Circuit-Breaker (2%)
        kill_switch = self.risk_manager.update_pnl(
            current_unrealized_pnl=self.portfolio.unrealized_pnl_today,
            capital=self.portfolio.current_capital,
        )
        if kill_switch:
            self.square_off_all_positions(reason=ExitReason.KILL_SWITCH)
            return

        # 3. Continuous Strategy Tick Evaluation (Trailing SL, Stop-Loss, Target hits)
        tick_signal = self.strategy.on_tick(price=price, timestamp=timestamp)
        if tick_signal and tick_signal.action == SignalAction.EXIT:
            self._execute_exit_signal(tick_signal)
            return

        # 4. Feed tick into 15m Candle Aggregator
        self.candle_aggregator.process_tick(price, volume, timestamp)

    def on_candle_completed(self, candle: dict, vwap: float):
        """Called automatically whenever a 15-minute candle completes."""
        logger.info(
            f"[{self.instrument.symbol}] 15m Candle Closed: {candle['datetime'].strftime('%H:%M')} | "
            f"O: {candle['open']:.2f}, H: {candle['high']:.2f}, L: {candle['low']:.2f}, "
            f"C: {candle['close']:.2f} | VWAP: {vwap:.2f}"
        )

        # Pass completed bar to strategy
        signal = self.strategy.on_candle(candle, vwap)
        if not signal:
            return

        if signal.action in (SignalAction.BUY, SignalAction.SELL):
            self._execute_entry_signal(signal)
        elif signal.action == SignalAction.EXIT:
            self._execute_exit_signal(signal)

    def _execute_entry_signal(self, signal: StrategySignal):
        """Pre-trade risk filtering and order routing for entries."""
        symbol = signal.symbol
        current_time = signal.timestamp.time()

        # Check if already in flight
        if self.order_manager.is_order_in_flight(symbol):
            logger.warning("Order already in flight. Skipping duplicate entry.")
            return

        stop_dist = abs(signal.price - signal.stop_loss) if signal.stop_loss else 50.0

        # Calculate Position Size
        margins = self.broker.get_margins()
        avail_margin = margins.get("available_cash", self.portfolio.current_capital)

        qty = self.position_sizer.calculate_order_quantity(
            capital=self.portfolio.current_capital,
            stop_distance=stop_dist,
            instrument=self.instrument,
            or_width=self.strategy.orb.width if self.strategy.orb else None,
            available_margin=avail_margin,
            estimated_price=signal.price,
        )

        # Pre-Trade Risk Gate
        approved, reason = self.risk_manager.validate_pre_trade(
            symbol=symbol,
            current_time=current_time,
            quantity=qty,
            capital=self.portfolio.current_capital,
            has_open_position=(self.strategy.position != 0),
        )

        if not approved:
            logger.warning(f"Risk gate rejection: {reason}")
            return

        # Route Marketable Limit Order (1 tick aggressive to guarantee fill while bounding slippage)
        direction = OrderDirection.BUY if signal.action == SignalAction.BUY else OrderDirection.SELL
        order_price = signal.price + 0.05 if direction == OrderDirection.BUY else signal.price - 0.05

        try:
            order_record = self.broker.place_order(
                symbol=symbol,
                direction=direction,
                order_type=OrderType.LIMIT,
                quantity=qty,
                price=order_price,
                tag="ORB_ENTRY",
            )
            self.order_manager.register_order(order_record)
            self.db.save_order(order_record)

            # Register Position in Strategy & Portfolio
            fill_price = order_record.average_fill_price or signal.price
            pos_mult = 1 if direction == OrderDirection.BUY else -1

            self.strategy.register_trade_entry(
                entry_price=fill_price,
                position=pos_mult,
                stop_loss=signal.stop_loss,
                target=signal.target,
                risk_dist=stop_dist,
            )
            self.risk_manager.record_trade_executed(symbol)
            self.portfolio.record_entry(symbol, direction.value, qty, fill_price)

            # Record in Trade Journal
            trade_id = f"TRD_{uuid.uuid4().hex[:8]}"
            self.current_trade = TradeRecord(
                trade_id=trade_id,
                symbol=symbol,
                direction=direction,
                entry_time=signal.timestamp,
                entry_price=fill_price,
                quantity=qty,
                initial_stop=signal.stop_loss,
                initial_target=signal.target,
                is_paper=(self.broker.name == "PAPER_BROKER"),
                notes=f"Signal: {signal.reason}",
            )
            self.db.record_trade_entry(self.current_trade)

        except Exception as e:
            logger.error(f"Failed to place entry order: {e}")

    def _execute_exit_signal(self, signal: StrategySignal):
        """Handles position closing and trade journaling."""
        if not self.current_trade or self.strategy.position == 0:
            return

        symbol = signal.symbol
        qty = self.current_trade.quantity
        exit_dir = OrderDirection.SELL if self.current_trade.direction == OrderDirection.BUY else OrderDirection.BUY

        try:
            order_record = self.broker.place_order(
                symbol=symbol,
                direction=exit_dir,
                order_type=OrderType.MARKET,
                quantity=qty,
                price=signal.price,
                tag="ORB_EXIT",
            )
            self.order_manager.register_order(order_record)
            self.db.save_order(order_record)

            exit_price = order_record.average_fill_price or signal.price

            # Calculate Gross & Net PnL with Oct 2024 SEBI friction
            if self.current_trade.direction == OrderDirection.BUY:
                gross_pnl = (exit_price - self.current_trade.entry_price) * qty
                buy_p, sell_p = self.current_trade.entry_price, exit_price
            else:
                gross_pnl = (self.current_trade.entry_price - exit_price) * qty
                buy_p, sell_p = exit_price, self.current_trade.entry_price

            costs = self.cost_calculator.calculate_trade_costs(
                symbol=symbol,
                instrument_type=self.instrument.instrument_type,
                buy_price=buy_p,
                sell_price=sell_p,
                quantity=qty,
            )
            net_pnl = round(gross_pnl - costs.total_cost, 2)

            # R-multiple
            initial_risk_amount = abs(self.current_trade.entry_price - self.current_trade.initial_stop) * qty
            r_mult = round(net_pnl / initial_risk_amount, 2) if initial_risk_amount > 0 else 0.0

            # Update Trade Journal
            self.current_trade.exit_time = signal.timestamp
            self.current_trade.exit_price = exit_price
            self.current_trade.exit_reason = ExitReason(signal.reason) if signal.reason in ExitReason.__members__ else ExitReason.MANUAL
            self.current_trade.pnl_gross = round(gross_pnl, 2)
            self.current_trade.pnl_net = net_pnl
            self.current_trade.total_costs = costs.total_cost
            self.current_trade.brokerage = costs.brokerage
            self.current_trade.stt = costs.stt
            self.current_trade.exchange_charges = costs.exchange_charges
            self.current_trade.gst = costs.gst
            self.current_trade.sebi_charges = costs.sebi_charges
            self.current_trade.stamp_duty = costs.stamp_duty
            self.current_trade.slippage = costs.slippage_cost
            self.current_trade.r_multiple = r_mult

            self.db.record_trade_exit(self.current_trade)

            # Update Portfolio & Strategy
            self.portfolio.record_exit(symbol, exit_price, net_pnl)
            self.strategy.register_trade_exit()
            self.current_trade = None

            logger.info(
                f"[{symbol}] Trade Closed: Gross ₹{gross_pnl:,.2f} | Frictions ₹{costs.total_cost:,.2f} | "
                f"Net PnL ₹{net_pnl:,.2f} ({r_mult}R) | Reason: {signal.reason}"
            )

        except Exception as e:
            logger.error(f"Failed to execute exit order: {e}")

    def square_off_all_positions(self, reason: ExitReason = ExitReason.TIME_SQUARE_OFF):
        """Mandatory square-off at 14:30 IST or upon circuit-breaker activation."""
        self.broker.cancel_all_orders(self.instrument.symbol)
        if self.strategy.position != 0 and self.current_trade:
            logger.info(f"Triggering square-off for {self.instrument.symbol}. Reason: {reason.value}")
            sig = StrategySignal(
                action=SignalAction.EXIT,
                symbol=self.instrument.symbol,
                timestamp=datetime.now(),
                price=self.strategy.entry_price,
                reason=reason.value,
            )
            self._execute_exit_signal(sig)
