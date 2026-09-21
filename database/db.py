"""
SQLite Database interface for persistent trade logging and order audit trails.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from database.models import ExitReason, OrderDirection, OrderRecord, OrderStatus, TradeRecord


class DatabaseManager:
    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            self.db_path = Path(__file__).resolve().parent / "trading_system.db"
        else:
            self.db_path = db_path

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
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

            # Orders table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                broker_order_id TEXT,
                client_order_id TEXT,
                signal_id TEXT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                order_type TEXT NOT NULL,
                price REAL,
                quantity INTEGER NOT NULL,
                status TEXT NOT NULL,
                filled_quantity INTEGER DEFAULT 0,
                average_fill_price REAL DEFAULT 0.0,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                reject_reason TEXT,
                tag TEXT
            );
            """)

            # Run migrations for existing DBs if needed
            cursor.execute("PRAGMA table_info(orders);")
            cols = [r[1] for r in cursor.fetchall()]
            if "client_order_id" not in cols:
                cursor.execute("ALTER TABLE orders ADD COLUMN client_order_id TEXT;")
            if "signal_id" not in cols:
                cursor.execute("ALTER TABLE orders ADD COLUMN signal_id TEXT;")

            # Daily summaries table
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

    def save_order(self, order: OrderRecord):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT OR REPLACE INTO orders (
                order_id, broker_order_id, client_order_id, signal_id, symbol, direction, order_type, price,
                quantity, status, filled_quantity, average_fill_price,
                created_at, updated_at, reject_reason, tag
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order.order_id,
                order.broker_order_id,
                order.client_order_id,
                order.signal_id,
                order.symbol,
                order.direction.value,
                order.order_type.value,
                order.price,
                order.quantity,
                order.status.value,
                order.filled_quantity,
                order.average_fill_price,
                order.created_at.isoformat(),
                order.updated_at.isoformat(),
                order.reject_reason,
                order.tag
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

    def get_orders(self, symbol: Optional[str] = None) -> List[dict]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM orders"
            params = []
            if symbol:
                query += " WHERE symbol = ?"
                params.append(symbol)
            query += " ORDER BY created_at DESC"
            cursor.execute(query, params)
            return [dict(r) for r in cursor.fetchall()]

    def get_all_trades(self) -> List[dict]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades ORDER BY entry_time DESC")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
