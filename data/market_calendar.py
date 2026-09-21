"""
NSE Trading Calendar & Intraday Session Phase Resolver.

This module was imported by run_algo.py but did not exist anywhere in the
repo, so `--mode backtest` / `--mode paper` crashed with ImportError before
a single line of strategy logic ran. It provides two things:

1. MarketCalendar.is_trading_day(date) — used by the historical data loader
   to skip weekends/exchange holidays when chunking date ranges for the
   Kite Historical API (calling the API for a holiday just wastes a request).
2. MarketCalendar.get_session_phase(time) — used by run_algo.py's live/paper
   loop and the CLI monitor to label where we are in the trading day.

The holiday list is the official NSE equity/derivatives trading-holiday
calendar. NSE publishes a new list every year (nseindia.com > Markets >
Trading Holidays) — update HOLIDAYS_BY_YEAR each December for the year
ahead. Nothing else in the codebase needs to change when you do.
"""

from datetime import date, time
from enum import Enum
from typing import Set


class SessionPhase(str, Enum):
    PRE_MARKET = "PRE_MARKET"
    ORB_FORMATION = "ORB_FORMATION"
    ENTRY_WINDOW = "ENTRY_WINDOW"
    POSITION_MANAGEMENT = "POSITION_MANAGEMENT"
    SQUARE_OFF = "SQUARE_OFF"
    CLOSED = "CLOSED"


# NSE full trading holidays (equity + F&O). Extend this every year.
HOLIDAYS_BY_YEAR = {
    2023: {
        date(2023, 1, 26), date(2023, 3, 7), date(2023, 3, 30), date(2023, 4, 4),
        date(2023, 4, 7), date(2023, 4, 14), date(2023, 4, 22), date(2023, 5, 1),
        date(2023, 6, 29), date(2023, 8, 15), date(2023, 9, 19), date(2023, 10, 2),
        date(2023, 10, 24), date(2023, 11, 14), date(2023, 11, 27), date(2023, 12, 25),
    },
    2024: {
        date(2024, 1, 22), date(2024, 1, 26), date(2024, 3, 8), date(2024, 3, 25),
        date(2024, 3, 29), date(2024, 4, 11), date(2024, 4, 17), date(2024, 5, 1),
        date(2024, 5, 20), date(2024, 6, 17), date(2024, 7, 17), date(2024, 8, 15),
        date(2024, 10, 2), date(2024, 11, 1), date(2024, 11, 15), date(2024, 12, 25),
    },
    2025: {
        date(2025, 2, 26), date(2025, 3, 14), date(2025, 3, 31), date(2025, 4, 10),
        date(2025, 4, 14), date(2025, 4, 18), date(2025, 5, 1), date(2025, 8, 15),
        date(2025, 8, 27), date(2025, 10, 2), date(2025, 10, 21), date(2025, 10, 22),
        date(2025, 11, 5), date(2025, 12, 25),
    },
    2026: {
        date(2026, 1, 26), date(2026, 3, 3), date(2026, 3, 25), date(2026, 4, 3),
        date(2026, 4, 14), date(2026, 5, 1), date(2026, 8, 15), date(2026, 10, 2),
        date(2026, 10, 20), date(2026, 11, 10), date(2026, 12, 25),
    },
}


# Union Budget days (Feb 1 annually, or interim budgets) and General Election counting days
BLACKOUT_DATES: Set[date] = {
    date(2019, 5, 23),  # 2019 General Election Results
    date(2024, 2, 1),   # 2024 Interim Budget
    date(2024, 6, 4),   # 2024 General Election Results
    date(2024, 7, 23),  # 2024 Full Union Budget
    date(2025, 2, 1),   # 2025 Union Budget
    date(2026, 2, 1),   # 2026 Union Budget
}


class MarketCalendar:
    @staticmethod
    def _all_holidays() -> Set[date]:
        merged: Set[date] = set()
        for holidays in HOLIDAYS_BY_YEAR.values():
            merged |= holidays
        return merged

    @staticmethod
    def is_trading_day(d: date) -> bool:
        """Weekday and not on the NSE holiday list. Falls back to weekday-only
        for years not yet in HOLIDAYS_BY_YEAR, so requests still work — just
        without holiday filtering for that year."""
        if d.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        if d in MarketCalendar._all_holidays():
            return False
        return True

    @staticmethod
    def is_blackout_date(d: date) -> bool:
        """Returns True if the date falls on Union Budget or Election Results day."""
        # Check explicit set or Feb 1 annual budget day
        if d in BLACKOUT_DATES or (d.month == 2 and d.day == 1):
            return True
        return False

    @staticmethod
    def get_session_phase(t: time) -> SessionPhase:
        if t < time(9, 15):
            return SessionPhase.PRE_MARKET
        if t < time(9, 45):
            return SessionPhase.ORB_FORMATION
        if t < time(13, 30):
            return SessionPhase.ENTRY_WINDOW
        if t < time(14, 30):
            return SessionPhase.POSITION_MANAGEMENT
        if t < time(15, 30):
            return SessionPhase.SQUARE_OFF
        return SessionPhase.CLOSED

