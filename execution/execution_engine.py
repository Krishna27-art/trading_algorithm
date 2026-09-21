"""
Production Execution Orchestrator.
Connects Market Data Ingestion -> Candle Aggregator -> Strategy -> Risk Gate -> Broker Gateway.
"""

import uuid
from datetime import datetime, time
from typing import Any, Dict, List, Optional

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
from strategy.base_strategy import BaseStrategy, SignalAction, StrategySignal
from strategy.orb_strategy import IntradayORBStrategy


class ExecutionEngine:
    def __init__(
        self,
        broker: BaseBrokerAdapter,
        instrument: InstrumentConfig,
        app_settings: AppSettings = settings,
        strategy: Optional[BaseStrategy] = None,
        db: Optional[DatabaseManager] = None,
        portfolio: Optional[PortfolioManager] = None,
        risk_manager: Optional[RiskManager] = None,
        order_manager: Optional[OrderManager] = None,
    ):
        self.broker = broker
        self.instrument = instrument
        self.settings = app_settings
        self.db = db or DatabaseManager(app_settings.db_path)

        # Core Components (shared across multi-instrument engines when provided)
        self.portfolio = portfolio or PortfolioManager(initial_capital=app_settings.risk.initial_capital)
        self.risk_manager = risk_manager or RiskManager(app_settings.risk)
        self.position_sizer = PositionSizer(app_settings.risk)
        self.cost_calculator = TransactionCostCalculator(app_settings.costs)
        self.order_manager = order_manager or OrderManager()

        if risk_manager is not None:
            logger.info(f"[{instrument.symbol}] Linked to shared portfolio-wide RiskManager.")

        # Strategy & Aggregator
        if strategy is not None:
            self.strategy = strategy
        else:
            strat_name = getattr(app_settings, "active_strategy", "cpr").lower()
            if strat_name == "cpr":
                from strategy.cpr_strategy import CPRRegimeBreakoutStrategy
                self.strategy = CPRRegimeBreakoutStrategy(instrument=self.instrument, strategy_config=app_settings.strategy)
            elif strat_name == "dual_ema":
                from strategy.dual_ema_strategy import BufferedDualEMAStrategy
                self.strategy = BufferedDualEMAStrategy(instrument=self.instrument, strategy_config=app_settings.strategy)
            else:
                self.strategy = IntradayORBStrategy(instrument=self.instrument, strategy_config=app_settings.strategy)

        self.candle_aggregator = CandleAggregator(
            symbol=instrument.symbol,
            timeframe_minutes=app_settings.strategy.candle_timeframe_minutes,
            on_candle_close=self.on_candle_completed,
        )

        # Active trade state
        self.current_trade: Optional[TradeRecord] = None
        self.is_reconciled: bool = False
        self.reconciliation_error: Optional[str] = None

    def reconcile_startup_state(self) -> Dict[str, Any]:
        """
        Reconciles broker positions and open orders with local state on startup.
        Prevents starting with 'no position' when a position exists or placing duplicate orders.
        """
        logger.info(f"[{self.instrument.symbol}] Initiating startup reconciliation...")
        report = {
            "symbol": self.instrument.symbol,
            "broker_position": 0,
            "local_trade_found": False,
            "action_taken": "NONE",
            "status": "OK",
        }

        try:
            # 1. Fetch broker positions
            broker_pos_list = self.broker.get_positions()
            broker_qty = 0
            broker_buy_price = 0.0
            for p in broker_pos_list:
                sym = p.get("symbol") or p.get("tradingsymbol")
                if sym == self.instrument.symbol:
                    broker_qty = int(p.get("quantity", 0))
                    broker_buy_price = float(p.get("buy_price") or p.get("average_price", 0.0))
                    break
            report["broker_position"] = broker_qty

            # 2. Fetch broker open orders
            broker_orders = self.broker.get_orders()
            for o in broker_orders:
                sym = o.get("symbol") or o.get("tradingsymbol")
                if sym == self.instrument.symbol:
                    status_str = o.get("status", "OPEN")
                    if status_str in ("OPEN", "PENDING", "TRIGGER PENDING"):
                        logger.warning(f"Found active broker order on startup: {o.get('order_id')}")

            # 3. Fetch local open trade from database
            open_trades = self.db.get_open_trades(self.instrument.symbol)
            local_trade = open_trades[0] if open_trades else None
            report["local_trade_found"] = local_trade is not None

            # 4. Compare and reconcile
            if broker_qty != 0 and local_trade:
                logger.info(f"Reconciled existing open trade {local_trade['trade_id']} with broker position {broker_qty}.")
                direction = OrderDirection(local_trade["direction"])
                pos_mult = 1 if direction == OrderDirection.BUY else -1
                self.strategy.register_trade_entry(
                    entry_price=local_trade["entry_price"],
                    position=pos_mult,
                    stop_loss=local_trade["initial_stop"],
                    target=local_trade["initial_target"],
                    risk_dist=abs(local_trade["entry_price"] - local_trade["initial_stop"]),
                )
                self.portfolio.record_entry(
                    self.instrument.symbol,
                    local_trade["direction"],
                    abs(broker_qty),
                    local_trade["entry_price"],
                )
                self.current_trade = TradeRecord(**local_trade)
                report["action_taken"] = "RESTORED_FROM_LOCAL_AND_BROKER"

            elif broker_qty != 0 and not local_trade:
                logger.warning(f"CRITICAL: Broker has open position {broker_qty} for {self.instrument.symbol} but no local trade found!")
                direction = OrderDirection.BUY if broker_qty > 0 else OrderDirection.SELL
                pos_mult = 1 if broker_qty > 0 else -1
                trade_id = f"RECOVERED_{uuid.uuid4().hex[:8]}"
                recovered_trade = TradeRecord(
                    trade_id=trade_id,
                    symbol=self.instrument.symbol,
                    direction=direction,
                    entry_time=datetime.now(),
                    entry_price=broker_buy_price,
                    quantity=abs(broker_qty),
                    initial_stop=broker_buy_price * 0.99 if pos_mult > 0 else broker_buy_price * 1.01,
                    initial_target=broker_buy_price * 1.02 if pos_mult > 0 else broker_buy_price * 0.98,
                    is_paper=(self.broker.name == "PAPER_BROKER"),
                    notes="Auto-recovered from broker position on restart",
                )
                self.db.record_trade_entry(recovered_trade)
                self.current_trade = recovered_trade
                self.strategy.register_trade_entry(
                    entry_price=broker_buy_price,
                    position=pos_mult,
                    stop_loss=recovered_trade.initial_stop,
                    target=recovered_trade.initial_target,
                    risk_dist=abs(broker_buy_price - recovered_trade.initial_stop),
                )
                self.portfolio.record_entry(
                    self.instrument.symbol,
                    direction.value,
                    abs(broker_qty),
                    broker_buy_price,
                )
                report["action_taken"] = "RECOVERED_FROM_BROKER"

            elif broker_qty == 0 and local_trade:
                logger.warning(f"Local trade {local_trade['trade_id']} was open, but broker position is 0. Closing local trade.")
                closed_trade = TradeRecord(**local_trade)
                closed_trade.exit_time = datetime.now()
                closed_trade.exit_price = local_trade["entry_price"]
                closed_trade.exit_reason = ExitReason.MANUAL
                closed_trade.notes = "Closed during startup reconciliation: broker position was zero"
                self.db.record_trade_exit(closed_trade)
                self.current_trade = None
                self.strategy.position = 0
                report["action_taken"] = "CLOSED_LOCAL_GHOST_TRADE"

            else:
                logger.info(f"Reconciliation clean: No open positions on broker or local DB for {self.instrument.symbol}.")
                report["action_taken"] = "CLEAN"

            self.is_reconciled = True
            self.reconciliation_error = None
            return report

        except Exception as e:
            logger.error(f"Startup reconciliation failed: {e}")
            self.is_reconciled = False
            self.reconciliation_error = str(e)
            report["status"] = "ERROR"
            report["error"] = str(e)
            return report

    def start(self):
        """Initializes broker connection and reconciles state."""
        logger.info(f"Starting Execution Engine in [{self.broker.name}] mode for {self.instrument.symbol}.")
        self.broker.connect()
        today = datetime.now().date()
        if self.risk_manager.current_trading_date != today:
            self.risk_manager.reset_daily_state(today)
            self.portfolio.reset_day()
        self.strategy.reset_session(today)
        self.candle_aggregator.reset_daily_session()

        # Reconcile Startup State
        self.reconcile_startup_state()

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

        # Invariant 11: No order placed if broker state is uncertain or reconciliation failed
        if not self.is_reconciled or self.reconciliation_error:
            logger.error(
                f"[{symbol}] Order blocked: Broker state uncertain or reconciliation failed: {self.reconciliation_error}"
            )
            return

        # Invariant 4 & Invariant 10: Signal Idempotency & Order deduplication
        if signal.signal_id and self.order_manager.is_duplicate_signal(signal.signal_id):
            logger.warning(f"[{symbol}] Signal {signal.signal_id} already processed. Skipping duplicate entry.")
            return

        # Check if already in flight or position exists
        if self.order_manager.is_order_in_flight(symbol) or self.strategy.position != 0:
            logger.warning(f"[{symbol}] Order already in flight or position exists. Skipping entry.")
            return

        stop_dist = abs(signal.price - signal.stop_loss) if signal.stop_loss else 50.0

        # Calculate Position Size
        margins = self.broker.get_margins()
        avail_margin = margins.get("available_cash", self.portfolio.current_capital)

        qty = self.position_sizer.calculate_order_quantity(
            capital=self.portfolio.current_capital,
            stop_distance=stop_dist,
            instrument=self.instrument,
            or_width=getattr(getattr(self.strategy, "orb", None), "width", None),
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
        client_order_id = f"CLT_{signal.signal_id or uuid.uuid4().hex[:12]}"

        try:
            strat_label = getattr(self.strategy, "name", "ENTRY")
            tag = f"{symbol[:8]}_{strat_label[:8]}"

            order_record = self.broker.place_order(
                symbol=symbol,
                direction=direction,
                order_type=OrderType.LIMIT,
                quantity=qty,
                price=order_price,
                tag=tag,
                client_order_id=client_order_id,
            )
            order_record.client_order_id = client_order_id
            order_record.signal_id = signal.signal_id
            self.order_manager.register_order(order_record)
            self.db.save_order(order_record)

            # Invariant: If order is rejected by broker, do NOT create position
            if order_record.status == OrderStatus.REJECTED:
                logger.warning(
                    f"[{symbol}] Entry order was REJECTED by broker: {order_record.reject_reason}. Position not registered."
                )
                return

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
            # Mark order status UNKNOWN and require manual/startup reconciliation
            unknown_order = OrderRecord(
                order_id=f"UNKNOWN_{uuid.uuid4().hex[:8]}",
                client_order_id=client_order_id,
                signal_id=signal.signal_id,
                symbol=symbol,
                direction=direction,
                order_type=OrderType.LIMIT,
                price=order_price,
                quantity=qty,
                status=OrderStatus.UNKNOWN,
                reject_reason=str(e),
            )
            try:
                self.order_manager.register_order(unknown_order)
                self.db.save_order(unknown_order)
            except Exception:
                pass
            self.is_reconciled = False
            self.reconciliation_error = f"Order in UNKNOWN state: {e}"
            raise

    def _execute_exit_signal(self, signal: StrategySignal):
        """Handles position closing and trade journaling."""
        if not self.current_trade or self.strategy.position == 0:
            return

        symbol = signal.symbol
        qty = self.current_trade.quantity
        exit_dir = OrderDirection.SELL if self.current_trade.direction == OrderDirection.BUY else OrderDirection.BUY
        client_order_id = f"CLT_EXIT_{uuid.uuid4().hex[:8]}"

        try:
            order_record = self.broker.place_order(
                symbol=symbol,
                direction=exit_dir,
                order_type=OrderType.MARKET,
                quantity=qty,
                price=signal.price,
                tag="ORB_EXIT",
                client_order_id=client_order_id,
            )
            order_record.client_order_id = client_order_id
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

            # Update Risk Manager with realized PnL delta for daily circuit breaker
            self.risk_manager.update_pnl(realized_pnl_delta=net_pnl, capital=self.portfolio.current_capital)

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
