"""
Central timezone utilities for the trading system.

All wall-clock/session decisions use Asia/Kolkata explicitly.
Naive datetimes returned by now_ist_naive() are IST by contract.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    """Return the current timezone-aware timestamp in Asia/Kolkata."""
    return datetime.now(IST)


def now_ist_naive() -> datetime:
    """
    Return current Asia/Kolkata time as a naive datetime.

    The naive value is intentionally used only where existing strategy/
    calendar APIs expect naive IST datetimes.
    """
    return now_ist().replace(tzinfo=None)


def today_ist() -> date:
    """Return today's calendar date in Asia/Kolkata."""
    return now_ist().date()


def now_ist_iso() -> str:
    """Return current Asia/Kolkata timestamp with explicit +05:30 offset."""
    return now_ist().isoformat()


def to_ist_aware(dt: Any) -> Optional[datetime]:
    """
    Convert any timestamp (datetime, ISO string, epoch) to timezone-aware Asia/Kolkata.
    Safely bridges naive and aware timestamps, assuming naive timestamps from NSE/Kite are IST.
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def to_ist_naive(dt: Any) -> Optional[datetime]:
    """Convert any timestamp to timezone-naive IST datetime."""
    aware = to_ist_aware(dt)
    if aware is None:
        return None
    return aware.replace(tzinfo=None)


from datetime import date, datetime, time
from enum import Enum
from typing import Any, Optional, Set



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
        date(2026, 1, 15),
        date(2026, 1, 26),
        date(2026, 3, 3),
        date(2026, 3, 26),
        date(2026, 3, 31),
        date(2026, 4, 3),
        date(2026, 4, 14),
        date(2026, 5, 1),
        date(2026, 5, 28),
        date(2026, 6, 26),
        date(2026, 9, 14),
        date(2026, 10, 2),
        date(2026, 10, 20),
        date(2026, 11, 10),
        date(2026, 11, 24),
        date(2026, 12, 25),
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

