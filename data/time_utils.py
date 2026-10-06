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
