"""
SQLite Database interface for persistent trade logging and order audit trails.
"""

import json
import logging
import math
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from enum import Enum
from pydantic import BaseModel, Field

logger = logging.getLogger("backend.database.db")


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


class SignalOutcome(str, Enum):
    PENDING = "PENDING"
    WIN = "WIN"
    LOSS = "LOSS"
    EXPIRED = "EXPIRED"
    INVALID = "INVALID"
    AMBIGUOUS = "AMBIGUOUS"


class SignalStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class SignalEventRecord(BaseModel):
    signal_id: str
    trading_date: str
    symbol: str
    instrument_token: Optional[int] = None
    strategy: str
    direction: str  # "LONG" or "SHORT"
    generated_at: datetime
    candle_timestamp: Optional[datetime] = None
    entry_price: float
    stop_loss: float
    target: float
    signal_status: str = "ACTIVE"
    outcome: SignalOutcome = SignalOutcome.PENDING
    outcome_time: Optional[datetime] = None
    outcome_price: Optional[float] = None
    return_pct: Optional[float] = None
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    peak_high: Optional[float] = None
    trough_low: Optional[float] = None
    minutes_to_outcome: Optional[float] = None
    notes: Optional[str] = None


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



class DatabaseManager:
    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            self.db_path = Path(__file__).resolve().parent / "trading_system.db"
        else:
            self.db_path = db_path

        self._lock = threading.RLock()
        self._active_signals: Dict[str, dict] = {}

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()
        self._load_active_signals()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=30000;")
        except sqlite3.OperationalError:
            pass
        return conn

    def _init_tables(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Trade journal table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT UNIQUE NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_time TIMESTAMP NOT NULL,
                entry_price REAL NOT NULL,
                exit_time TIMESTAMP,
                exit_price REAL,
                quantity INTEGER NOT NULL,
                initial_stop REAL NOT NULL,
                initial_target REAL NOT NULL,
                exit_reason TEXT,
                pnl_gross REAL DEFAULT 0.0,
                pnl_net REAL DEFAULT 0.0,
                total_costs REAL DEFAULT 0.0,
                brokerage REAL DEFAULT 0.0,
                stt REAL DEFAULT 0.0,
                exchange_charges REAL DEFAULT 0.0,
                gst REAL DEFAULT 0.0,
                sebi_charges REAL DEFAULT 0.0,
                stamp_duty REAL DEFAULT 0.0,
                slippage REAL DEFAULT 0.0,
                r_multiple REAL DEFAULT 0.0,
                is_paper INTEGER DEFAULT 1,
                notes TEXT
            );
            """)


            # Signal journal table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS signal_events (
                signal_id TEXT PRIMARY KEY,
                trading_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                instrument_token INTEGER,
                strategy TEXT NOT NULL,
                direction TEXT NOT NULL,
                generated_at TIMESTAMP NOT NULL,
                candle_timestamp TIMESTAMP,
                entry_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                target REAL NOT NULL,
                signal_status TEXT NOT NULL DEFAULT 'ACTIVE',
                outcome TEXT NOT NULL DEFAULT 'PENDING',
                outcome_time TIMESTAMP,
                outcome_price REAL,
                return_pct REAL,
                mfe_pct REAL DEFAULT 0.0,
                mae_pct REAL DEFAULT 0.0,
                peak_high REAL,
                trough_low REAL,
                minutes_to_outcome REAL,
                notes TEXT
            );
            """)

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_strat_sym ON signal_events (strategy, symbol, signal_status);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_date ON signal_events (trading_date);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_generated ON signal_events (generated_at);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_status ON signal_events (signal_status);")

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_summaries (
                date TEXT PRIMARY KEY,
                starting_capital REAL NOT NULL,
                ending_capital REAL NOT NULL,
                pnl_gross REAL NOT NULL,
                pnl_net REAL NOT NULL,
                total_trades INTEGER NOT NULL,
                win_count INTEGER NOT NULL,
                loss_count INTEGER NOT NULL,
                kill_switch_triggered INTEGER DEFAULT 0
            );
            """)
            conn.commit()

    def record_trade_entry(self, trade: TradeRecord):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT OR REPLACE INTO trades (
                trade_id, symbol, direction, entry_time, entry_price, quantity,
                initial_stop, initial_target, is_paper, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.trade_id,
                trade.symbol,
                trade.direction.value,
                trade.entry_time.isoformat(),
                trade.entry_price,
                trade.quantity,
                trade.initial_stop,
                trade.initial_target,
                1 if trade.is_paper else 0,
                trade.notes
            ))
            conn.commit()

    def record_trade_exit(self, trade: TradeRecord):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE trades SET
                exit_time = ?,
                exit_price = ?,
                exit_reason = ?,
                pnl_gross = ?,
                pnl_net = ?,
                total_costs = ?,
                brokerage = ?,
                stt = ?,
                exchange_charges = ?,
                gst = ?,
                sebi_charges = ?,
                stamp_duty = ?,
                slippage = ?,
                r_multiple = ?,
                notes = ?
            WHERE trade_id = ?
            """, (
                trade.exit_time.isoformat() if trade.exit_time else None,
                trade.exit_price,
                trade.exit_reason.value if trade.exit_reason else None,
                trade.pnl_gross,
                trade.pnl_net,
                trade.total_costs,
                trade.brokerage,
                trade.stt,
                trade.exchange_charges,
                trade.gst,
                trade.sebi_charges,
                trade.stamp_duty,
                trade.slippage,
                trade.r_multiple,
                trade.notes,
                trade.trade_id
            ))
            conn.commit()

    def get_open_trades(self, symbol: Optional[str] = None) -> List[dict]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM trades WHERE exit_time IS NULL"
            params = []
            if symbol:
                query += " AND symbol = ?"
                params.append(symbol)
            query += " ORDER BY entry_time DESC"
            cursor.execute(query, params)
            return [dict(r) for r in cursor.fetchall()]


    def get_all_trades(self) -> List[dict]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades ORDER BY entry_time DESC")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_live_trades(self) -> List[dict]:
        """
        Returns only non-paper/live journal records.

        Backtests and simulated trades are stored with is_paper=1 and are
        intentionally excluded from live-facing views.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT *
                FROM trades
                WHERE is_paper = 0
                ORDER BY entry_time DESC
                """
            )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_open_live_trades(self, symbol: Optional[str] = None) -> List[dict]:
        """
        Returns only open, non-paper/live journal records.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = """
                SELECT *
                FROM trades
                WHERE is_paper = 0
                  AND exit_time IS NULL
            """
            params = []

            if symbol:
                query += " AND symbol = ?"
                params.append(symbol)

            query += " ORDER BY entry_time DESC"

            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    # =========================================================================
    # SIGNAL JOURNAL (SIGNAL-ONLY PERSISTENCE & OUTCOME TRACKING)
    # =========================================================================

    def _load_active_signals(self) -> None:
        """Load currently active (pending) signals into in-memory cache on startup."""
        with self._lock:
            self._active_signals.clear()
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT * FROM signal_events
                    WHERE signal_status = 'ACTIVE' AND outcome = 'PENDING'
                """)
                rows = cursor.fetchall()
                for r in rows:
                    d = dict(r)
                    self._active_signals[d["signal_id"]] = d

    def record_signal(self, signal: SignalEventRecord) -> bool:
        """
        Persists a newly generated actionable signal if not duplicate.
        Returns True if newly recorded, False if deduplicated/ignored.
        """
        clean_symbol = signal.symbol.strip().upper()
        clean_strat = signal.strategy.strip().lower()

        with self._lock:
            # Check 1: In-memory active check for (strategy, symbol)
            for active in self._active_signals.values():
                if (
                    active.get("strategy", "").lower() == clean_strat
                    and active.get("symbol", "").upper() == clean_symbol
                    and active.get("trading_date") == signal.trading_date
                    and active.get("signal_status") == "ACTIVE"
                ):
                    # Active pending signal already running for this stock/strategy today
                    return False

            # Check 2: Database deduplication
            with self._get_connection() as conn:
                cursor = conn.cursor()

                # Check if exact signal_id already exists
                cursor.execute(
                    "SELECT signal_id FROM signal_events WHERE signal_id = ?",
                    (signal.signal_id,)
                )
                if cursor.fetchone():
                    return False

                # Check if duplicate candle-close signal exists for this candle
                if signal.candle_timestamp is not None:
                    candle_iso = signal.candle_timestamp.isoformat() if hasattr(signal.candle_timestamp, "isoformat") else str(signal.candle_timestamp)
                    cursor.execute("""
                        SELECT signal_id FROM signal_events
                        WHERE strategy = ? AND symbol = ? AND candle_timestamp = ?
                    """, (clean_strat, clean_symbol, candle_iso))
                    if cursor.fetchone():
                        return False

                # Check if active pending signal exists in database
                cursor.execute("""
                    SELECT signal_id FROM signal_events
                    WHERE strategy = ? AND symbol = ? AND trading_date = ? AND signal_status = 'ACTIVE'
                """, (clean_strat, clean_symbol, signal.trading_date))
                if cursor.fetchone():
                    return False

                # Insert new signal event
                candle_ts_val = (
                    signal.candle_timestamp.isoformat()
                    if signal.candle_timestamp is not None and hasattr(signal.candle_timestamp, "isoformat")
                    else (str(signal.candle_timestamp) if signal.candle_timestamp is not None else None)
                )
                gen_ts_val = (
                    signal.generated_at.isoformat()
                    if hasattr(signal.generated_at, "isoformat")
                    else str(signal.generated_at)
                )

                peak = signal.peak_high or signal.entry_price
                trough = signal.trough_low or signal.entry_price

                cursor.execute("""
                    INSERT INTO signal_events (
                        signal_id, trading_date, symbol, instrument_token, strategy,
                        direction, generated_at, candle_timestamp, entry_price,
                        stop_loss, target, signal_status, outcome, return_pct,
                        mfe_pct, mae_pct, peak_high, trough_low, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    signal.signal_id,
                    signal.trading_date,
                    clean_symbol,
                    signal.instrument_token,
                    clean_strat,
                    signal.direction,
                    gen_ts_val,
                    candle_ts_val,
                    signal.entry_price,
                    signal.stop_loss,
                    signal.target,
                    signal.signal_status,
                    signal.outcome.value if hasattr(signal.outcome, "value") else str(signal.outcome),
                    signal.return_pct,
                    signal.mfe_pct,
                    signal.mae_pct,
                    peak,
                    trough,
                    signal.notes,
                ))
                conn.commit()

            # Cache in-memory active tracking
            if signal.signal_status == "ACTIVE" and signal.outcome == SignalOutcome.PENDING:
                self._active_signals[signal.signal_id] = {
                    "signal_id": signal.signal_id,
                    "trading_date": signal.trading_date,
                    "symbol": clean_symbol,
                    "instrument_token": signal.instrument_token,
                    "strategy": clean_strat,
                    "direction": signal.direction,
                    "generated_at": gen_ts_val,
                    "candle_timestamp": candle_ts_val,
                    "entry_price": float(signal.entry_price),
                    "stop_loss": float(signal.stop_loss),
                    "target": float(signal.target),
                    "signal_status": "ACTIVE",
                    "outcome": "PENDING",
                    "peak_high": float(peak),
                    "trough_low": float(trough),
                    "mfe_pct": float(signal.mfe_pct),
                    "mae_pct": float(signal.mae_pct),
                    "notes": signal.notes,
                }

            logger.info(
                "[DatabaseManager] Recorded signal %s %s %s entry=%.2f stop=%.2f target=%.2f",
                clean_strat, clean_symbol, signal.direction, signal.entry_price, signal.stop_loss, signal.target
            )
            return True

    def update_active_signal_tick(
        self,
        symbol: str,
        price: float,
        timestamp: datetime,
    ) -> List[dict]:
        """
        Evaluates active signals for the symbol against a real incoming tick.
        Updates peak MFE/MAE and resolves target or stop breach.
        """
        clean_symbol = symbol.strip().upper()
        if not math.isfinite(price) or price <= 0:
            return []

        resolved_signals: List[dict] = []

        with self._lock:
            active_ids = [
                sid for sid, sig in self._active_signals.items()
                if sig.get("symbol") == clean_symbol
            ]
            if not active_ids:
                return []

            iso_now = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)

            for sid in active_ids:
                sig = self._active_signals[sid]
                entry = sig["entry_price"]
                stop = sig["stop_loss"]
                target = sig["target"]
                direction = sig["direction"]

                # Update extremes
                sig["peak_high"] = max(sig.get("peak_high", entry), price)
                sig["trough_low"] = min(sig.get("trough_low", entry), price)
                peak = sig["peak_high"]
                trough = sig["trough_low"]

                # Calculate excursion metrics
                if direction == "LONG":
                    mfe = round(((peak - entry) / entry) * 100.0, 2)
                    mae = round(((trough - entry) / entry) * 100.0, 2)  # negative or 0
                else:  # SHORT
                    mfe = round(((entry - trough) / entry) * 100.0, 2)
                    mae = round(((entry - peak) / entry) * 100.0, 2)  # negative or 0

                sig["mfe_pct"] = max(sig.get("mfe_pct", 0.0), mfe)
                sig["mae_pct"] = min(sig.get("mae_pct", 0.0), mae)

                # Check outcome breach
                outcome = None
                outcome_price = None

                if direction == "LONG":
                    if price >= target:
                        outcome = "WIN"
                        outcome_price = price
                    elif price <= stop:
                        outcome = "LOSS"
                        outcome_price = price
                elif direction == "SHORT":
                    if price <= target:
                        outcome = "WIN"
                        outcome_price = price
                    elif price >= stop:
                        outcome = "LOSS"
                        outcome_price = price

                if outcome is not None:
                    # Signal concluded
                    if direction == "LONG":
                        ret_pct = round(((outcome_price - entry) / entry) * 100.0, 2)
                    else:
                        ret_pct = round(((entry - outcome_price) / entry) * 100.0, 2)

                    try:
                        gen_dt = datetime.fromisoformat(sig["generated_at"]) if isinstance(sig["generated_at"], str) else sig["generated_at"]
                        cur_dt = timestamp if isinstance(timestamp, datetime) else datetime.fromisoformat(str(timestamp))
                        mins = round(max(0.0, (cur_dt - gen_dt).total_seconds() / 60.0), 1)
                    except Exception:
                        mins = None

                    sig["outcome"] = outcome
                    sig["outcome_time"] = iso_now
                    sig["outcome_price"] = outcome_price
                    sig["return_pct"] = ret_pct
                    sig["signal_status"] = "CLOSED"
                    sig["minutes_to_outcome"] = mins

                    # Write resolution to SQLite
                    with self._get_connection() as conn:
                        cursor = conn.cursor()
                        cursor.execute("""
                            UPDATE signal_events SET
                                signal_status = 'CLOSED',
                                outcome = ?,
                                outcome_time = ?,
                                outcome_price = ?,
                                return_pct = ?,
                                mfe_pct = ?,
                                mae_pct = ?,
                                peak_high = ?,
                                trough_low = ?,
                                minutes_to_outcome = ?
                            WHERE signal_id = ?
                        """, (
                            outcome,
                            iso_now,
                            outcome_price,
                            ret_pct,
                            sig["mfe_pct"],
                            sig["mae_pct"],
                            sig["peak_high"],
                            sig["trough_low"],
                            mins,
                            sid
                        ))
                        conn.commit()

                    resolved_signals.append(dict(sig))
                    del self._active_signals[sid]
                    logger.info(
                        "[DatabaseManager] Signal %s resolved: %s outcome=%s price=%.2f ret=%.2f%% MFE=%.2f%% MAE=%.2f%%",
                        sid, direction, outcome, outcome_price, ret_pct, sig["mfe_pct"], sig["mae_pct"]
                    )

        return resolved_signals

    def update_active_signals_candle(
        self,
        symbol: str,
        candle: dict,
    ) -> List[dict]:
        """
        Evaluates active signals on a completed candle.
        Conservative ambiguity rule: if both target and stop are breached
        within the same candle, outcome is marked AMBIGUOUS.
        """
        clean_symbol = symbol.strip().upper()
        high = candle.get("high")
        low = candle.get("low")
        if high is None or low is None or high <= 0 or low <= 0:
            return []

        resolved_signals: List[dict] = []
        candle_dt = candle.get("datetime")
        iso_time = candle_dt.isoformat() if hasattr(candle_dt, "isoformat") else str(candle_dt or "")

        with self._lock:
            active_ids = [
                sid for sid, sig in self._active_signals.items()
                if sig.get("symbol") == clean_symbol
            ]
            if not active_ids:
                return []

            for sid in active_ids:
                sig = self._active_signals[sid]
                entry = sig["entry_price"]
                stop = sig["stop_loss"]
                target = sig["target"]
                direction = sig["direction"]

                # Update extremes
                sig["peak_high"] = max(sig.get("peak_high", entry), float(high))
                sig["trough_low"] = min(sig.get("trough_low", entry), float(low))
                peak = sig["peak_high"]
                trough = sig["trough_low"]

                if direction == "LONG":
                    sig["mfe_pct"] = max(sig.get("mfe_pct", 0.0), round(((peak - entry) / entry) * 100.0, 2))
                    sig["mae_pct"] = min(sig.get("mae_pct", 0.0), round(((trough - entry) / entry) * 100.0, 2))
                    target_hit = high >= target
                    stop_hit = low <= stop
                else:  # SHORT
                    sig["mfe_pct"] = max(sig.get("mfe_pct", 0.0), round(((entry - trough) / entry) * 100.0, 2))
                    sig["mae_pct"] = min(sig.get("mae_pct", 0.0), round(((entry - peak) / entry) * 100.0, 2))
                    target_hit = low <= target
                    stop_hit = high >= stop

                outcome = None
                outcome_price = None
                ret_pct = 0.0

                if target_hit and stop_hit:
                    # Ambiguous candle: both breached within the same bar
                    outcome = "AMBIGUOUS"
                    outcome_price = entry
                    ret_pct = 0.0
                elif target_hit:
                    outcome = "WIN"
                    outcome_price = target
                    ret_pct = round(((target - entry) / entry) * 100.0, 2) if direction == "LONG" else round(((entry - target) / entry) * 100.0, 2)
                elif stop_hit:
                    outcome = "LOSS"
                    outcome_price = stop
                    ret_pct = round(((stop - entry) / entry) * 100.0, 2) if direction == "LONG" else round(((entry - stop) / entry) * 100.0, 2)

                if outcome is not None:
                    try:
                        gen_dt = datetime.fromisoformat(sig["generated_at"]) if isinstance(sig["generated_at"], str) else sig["generated_at"]
                        cur_dt = candle_dt if isinstance(candle_dt, datetime) else datetime.fromisoformat(str(candle_dt))
                        mins = round(max(0.0, (cur_dt - gen_dt).total_seconds() / 60.0), 1)
                    except Exception:
                        mins = None

                    sig["outcome"] = outcome
                    sig["outcome_time"] = iso_time
                    sig["outcome_price"] = outcome_price
                    sig["return_pct"] = ret_pct
                    sig["signal_status"] = "CLOSED"
                    sig["minutes_to_outcome"] = mins

                    with self._get_connection() as conn:
                        cursor = conn.cursor()
                        cursor.execute("""
                            UPDATE signal_events SET
                                signal_status = 'CLOSED',
                                outcome = ?,
                                outcome_time = ?,
                                outcome_price = ?,
                                return_pct = ?,
                                mfe_pct = ?,
                                mae_pct = ?,
                                peak_high = ?,
                                trough_low = ?,
                                minutes_to_outcome = ?
                            WHERE signal_id = ?
                        """, (
                            outcome,
                            iso_time,
                            outcome_price,
                            ret_pct,
                            sig["mfe_pct"],
                            sig["mae_pct"],
                            sig["peak_high"],
                            sig["trough_low"],
                            mins,
                            sid
                        ))
                        conn.commit()

                    resolved_signals.append(dict(sig))
                    del self._active_signals[sid]
                    logger.info(
                        "[DatabaseManager] Signal %s resolved via candle: outcome=%s ret=%.2f%%",
                        sid, outcome, ret_pct
                    )

        return resolved_signals

    def finalize_session_signals(
        self,
        trading_date: Optional[str] = None,
        session_close_time: Optional[datetime] = None,
        current_prices: Optional[Dict[str, float]] = None,
    ) -> int:
        """
        At session end (15:30 IST), mark outstanding unresolved signals as EXPIRED.
        Idempotent: cannot finalize twice.
        """
        prices = current_prices or {}
        from backend.data.time_utils import now_ist_naive
        now_dt = session_close_time or now_ist_naive()
        iso_close = now_dt.isoformat() if hasattr(now_dt, "isoformat") else str(now_dt)
        date_str = trading_date or now_dt.strftime("%Y-%m-%d")

        count_finalized = 0

        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                query = "SELECT * FROM signal_events WHERE signal_status = 'ACTIVE' AND outcome = 'PENDING'"
                params = []
                if trading_date:
                    query += " AND trading_date = ?"
                    params.append(trading_date)

                cursor.execute(query, params)
                pending_rows = cursor.fetchall()

                for row in pending_rows:
                    sig = dict(row)
                    sid = sig["signal_id"]
                    entry = sig["entry_price"]
                    direction = sig["direction"]
                    sym = sig["symbol"]

                    raw_out_price = prices.get(sym)
                    if raw_out_price is not None and math.isfinite(raw_out_price) and raw_out_price > 0:
                        out_price = float(raw_out_price)
                        ret_pct = round(((out_price - entry) / entry) * 100.0, 2) if direction == "LONG" else round(((entry - out_price) / entry) * 100.0, 2)
                    else:
                        out_price = None
                        ret_pct = None

                    try:
                        gen_dt = datetime.fromisoformat(sig["generated_at"]) if isinstance(sig["generated_at"], str) else sig["generated_at"]
                        mins = round(max(0.0, (now_dt - gen_dt).total_seconds() / 60.0), 1)
                    except Exception:
                        mins = None

                    cursor.execute("""
                        UPDATE signal_events SET
                            signal_status = 'CLOSED',
                            outcome = 'EXPIRED',
                            outcome_time = ?,
                            outcome_price = ?,
                            return_pct = ?,
                            minutes_to_outcome = ?
                        WHERE signal_id = ?
                    """, (iso_close, out_price, ret_pct, mins, sid))
                    count_finalized += 1

                conn.commit()

            # Evict from active cache
            to_delete = [
                sid for sid, sig in self._active_signals.items()
                if (trading_date is None or sig.get("trading_date") == trading_date)
            ]
            for sid in to_delete:
                del self._active_signals[sid]

        if count_finalized > 0:
            logger.info(
                "[DatabaseManager] Session finalized: %d pending signals marked EXPIRED for %s",
                count_finalized, date_str
            )
        return count_finalized

    def get_pending_signals(self, symbol: Optional[str] = None) -> List[dict]:
        """Returns all currently active (pending) signals."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM signal_events WHERE signal_status = 'ACTIVE' AND outcome = 'PENDING'"
            params = []
            if symbol:
                query += " AND symbol = ?"
                params.append(symbol.strip().upper())
            query += " ORDER BY generated_at DESC"
            cursor.execute(query, params)
            return [dict(r) for r in cursor.fetchall()]

    def get_signal_history(
        self,
        limit: int = 100,
        strategy: Optional[str] = None,
        symbol: Optional[str] = None,
        period: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> List[dict]:
        """
        Returns recent signal journal events with optional filters.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM signal_events WHERE 1=1"
            params: List[Any] = []

            if strategy:
                query += " AND strategy = ?"
                params.append(strategy.strip().lower())
            if symbol:
                query += " AND symbol = ?"
                params.append(symbol.strip().upper())
            if outcome:
                query += " AND outcome = ?"
                params.append(outcome.strip().upper())

            if period:
                from backend.data.time_utils import now_ist_naive
                period_upper = period.strip().upper()
                today = now_ist_naive().date()
                if period_upper == "TODAY":
                    query += " AND trading_date = ?"
                    params.append(today.strftime("%Y-%m-%d"))
                elif period_upper == "WEEK":
                    week_ago = (today - timedelta(days=7)).strftime("%Y-%m-%d")
                    query += " AND trading_date >= ?"
                    params.append(week_ago)
                elif period_upper == "MONTH":
                    month_ago = (today - timedelta(days=30)).strftime("%Y-%m-%d")
                    query += " AND trading_date >= ?"
                    params.append(month_ago)
                elif period_upper == "YEAR":
                    year_ago = (today - timedelta(days=365)).strftime("%Y-%m-%d")
                    query += " AND trading_date >= ?"
                    params.append(year_ago)

            query += " ORDER BY generated_at DESC LIMIT ?"
            params.append(max(1, min(limit, 1000)))

            cursor.execute(query, params)
            return [dict(r) for r in cursor.fetchall()]

    def get_strategy_performance(
        self,
        period: str = "TODAY",
        strategy: Optional[str] = None,
        symbol: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Dynamically computes strategy accuracy and excursion metrics directly
        from signal_events via SQL aggregation. Does NOT store hardcoded percentages.
        """
        from backend.data.time_utils import now_ist_naive
        period_upper = (period or "TODAY").strip().upper()
        today = now_ist_naive().date()
        today_str = today.strftime("%Y-%m-%d")

        where_clauses = ["1=1"]
        params: List[Any] = []

        if period_upper == "TODAY":
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM signal_events WHERE trading_date = ? LIMIT 1", (today_str,))
                if not cursor.fetchone():
                    cursor.execute("SELECT MAX(trading_date) FROM signal_events")
                    max_row = cursor.fetchone()
                    if max_row and max_row[0]:
                        today_str = max_row[0]
            where_clauses.append("trading_date = ?")
            params.append(today_str)
        elif period_upper == "WEEK":
            week_ago = (today - timedelta(days=7)).strftime("%Y-%m-%d")
            where_clauses.append("trading_date >= ?")
            params.append(week_ago)
        elif period_upper == "MONTH":
            month_ago = (today - timedelta(days=30)).strftime("%Y-%m-%d")
            where_clauses.append("trading_date >= ?")
            params.append(month_ago)
        elif period_upper == "YEAR":
            year_ago = (today - timedelta(days=365)).strftime("%Y-%m-%d")
            where_clauses.append("trading_date >= ?")
            params.append(year_ago)

        if strategy:
            where_clauses.append("strategy = ?")
            params.append(strategy.strip().lower())

        if symbol:
            where_clauses.append("symbol = ?")
            params.append(symbol.strip().upper())

        if direction:
            where_clauses.append("direction = ?")
            params.append(direction.strip().upper())

        where_sql = " AND ".join(where_clauses)

        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Aggregate per strategy
            strat_query = f"""
                SELECT
                    strategy,
                    COUNT(*) as total_signals,
                    SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN outcome = 'EXPIRED' THEN 1 ELSE 0 END) as expired,
                    SUM(CASE WHEN outcome = 'AMBIGUOUS' THEN 1 ELSE 0 END) as ambiguous,
                    SUM(CASE WHEN outcome = 'PENDING' THEN 1 ELSE 0 END) as pending,
                    AVG(CASE WHEN outcome IN ('WIN', 'LOSS', 'EXPIRED') THEN return_pct ELSE NULL END) as avg_return,
                    AVG(mfe_pct) as avg_mfe,
                    AVG(mae_pct) as avg_mae,
                    SUM(CASE WHEN direction = 'LONG' THEN 1 ELSE 0 END) as long_signals,
                    SUM(CASE WHEN direction = 'LONG' AND outcome = 'WIN' THEN 1 ELSE 0 END) as long_wins,
                    SUM(CASE WHEN direction = 'LONG' AND outcome = 'LOSS' THEN 1 ELSE 0 END) as long_losses,
                    SUM(CASE WHEN direction = 'SHORT' THEN 1 ELSE 0 END) as short_signals,
                    SUM(CASE WHEN direction = 'SHORT' AND outcome = 'WIN' THEN 1 ELSE 0 END) as short_wins,
                    SUM(CASE WHEN direction = 'SHORT' AND outcome = 'LOSS' THEN 1 ELSE 0 END) as short_losses
                FROM signal_events
                WHERE {where_sql}
                GROUP BY strategy
                ORDER BY total_signals DESC
            """
            cursor.execute(strat_query, params)
            rows = cursor.fetchall()

            strategies_metrics = []
            total_signals_all = 0
            total_wins_all = 0
            total_losses_all = 0
            total_expired_all = 0

            for r in rows:
                strat_name = r["strategy"]
                signals_cnt = int(r["total_signals"] or 0)
                wins_cnt = int(r["wins"] or 0)
                losses_cnt = int(r["losses"] or 0)
                expired_cnt = int(r["expired"] or 0)
                ambiguous_cnt = int(r["ambiguous"] or 0)
                pending_cnt = int(r["pending"] or 0)

                total_signals_all += signals_cnt
                total_wins_all += wins_cnt
                total_losses_all += losses_cnt
                total_expired_all += expired_cnt

                # Resolved = Wins + Losses (Expired is kept separate to avoid distortion)
                resolved = wins_cnt + losses_cnt
                accuracy = round((wins_cnt / resolved) * 100.0, 1) if resolved > 0 else 0.0

                long_resolved = int(r["long_wins"] or 0) + int(r["long_losses"] or 0)
                long_acc = round((int(r["long_wins"] or 0) / long_resolved) * 100.0, 1) if long_resolved > 0 else 0.0

                short_resolved = int(r["short_wins"] or 0) + int(r["short_losses"] or 0)
                short_acc = round((int(r["short_wins"] or 0) / short_resolved) * 100.0, 1) if short_resolved > 0 else 0.0

                strat_metric = {
                    "strategy": strat_name,
                    "signals": signals_cnt,
                    "wins": wins_cnt,
                    "losses": losses_cnt,
                    "expired": expired_cnt,
                    "ambiguous": ambiguous_cnt,
                    "pending": pending_cnt,
                    "accuracy": accuracy,
                    "avg_return": round(float(r["avg_return"] or 0.0), 2),
                    "avg_mfe": round(float(r["avg_mfe"] or 0.0), 2),
                    "avg_mae": round(float(r["avg_mae"] or 0.0), 2),
                    "long_signals": int(r["long_signals"] or 0),
                    "long_wins": int(r["long_wins"] or 0),
                    "long_losses": int(r["long_losses"] or 0),
                    "long_accuracy": long_acc,
                    "short_signals": int(r["short_signals"] or 0),
                    "short_wins": int(r["short_wins"] or 0),
                    "short_losses": int(r["short_losses"] or 0),
                    "short_accuracy": short_acc,
                }

                # If single strategy drill-down, add breakdown by symbol
                if strategy or len(rows) == 1:
                    sym_query = f"""
                        SELECT
                            symbol,
                            COUNT(*) as total_signals,
                            SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) as wins,
                            SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END) as losses,
                            SUM(CASE WHEN outcome = 'EXPIRED' THEN 1 ELSE 0 END) as expired,
                            AVG(CASE WHEN outcome IN ('WIN', 'LOSS', 'EXPIRED') THEN return_pct ELSE NULL END) as avg_return,
                            AVG(mfe_pct) as avg_mfe,
                            AVG(mae_pct) as avg_mae
                        FROM signal_events
                        WHERE {where_sql} AND strategy = ?
                        GROUP BY symbol
                        ORDER BY total_signals DESC
                    """
                    cursor.execute(sym_query, params + [strat_name])
                    sym_rows = cursor.fetchall()
                    symbol_breakdown = []
                    for sr in sym_rows:
                        s_res = int(sr["wins"] or 0) + int(sr["losses"] or 0)
                        s_acc = round((int(sr["wins"] or 0) / s_res) * 100.0, 1) if s_res > 0 else 0.0
                        symbol_breakdown.append({
                            "symbol": sr["symbol"],
                            "signals": int(sr["total_signals"] or 0),
                            "wins": int(sr["wins"] or 0),
                            "losses": int(sr["losses"] or 0),
                            "expired": int(sr["expired"] or 0),
                            "accuracy": s_acc,
                            "avg_return": round(float(sr["avg_return"] or 0.0), 2),
                            "avg_mfe": round(float(sr["avg_mfe"] or 0.0), 2),
                            "avg_mae": round(float(sr["avg_mae"] or 0.0), 2),
                        })
                    strat_metric["symbol_breakdown"] = symbol_breakdown

                strategies_metrics.append(strat_metric)

            # Overall summary
            overall_resolved = total_wins_all + total_losses_all
            overall_acc = round((total_wins_all / overall_resolved) * 100.0, 1) if overall_resolved > 0 else 0.0

            return {
                "period": period_upper,
                "trading_date": today_str,
                "total_signals": total_signals_all,
                "total_wins": total_wins_all,
                "total_losses": total_losses_all,
                "total_expired": total_expired_all,
                "overall_accuracy": overall_acc,
                "strategies": strategies_metrics,
            }


db_manager = DatabaseManager()


