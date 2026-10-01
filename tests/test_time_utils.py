from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from data.time_utils import (
    IST,
    now_ist,
    now_ist_iso,
    now_ist_naive,
    today_ist,
)


def test_now_ist_has_india_timezone():
    value = now_ist()

    assert value.tzinfo is not None
    assert value.utcoffset() == timedelta(hours=5, minutes=30)
    assert value.tzinfo == IST


def test_now_ist_naive_is_still_india_wall_clock_time():
    aware = now_ist()
    naive = now_ist_naive()

    assert naive.tzinfo is None

    assert naive.date() == aware.date()
    assert naive.hour == aware.hour
    assert naive.minute == aware.minute


def test_today_ist_returns_india_calendar_date():
    assert today_ist() == now_ist().date()


def test_now_ist_iso_contains_explicit_offset():
    timestamp = now_ist_iso()

    assert timestamp.endswith("+05:30")


def test_strategy_state_uses_ist_wall_clock(monkeypatch):
    from backend import signals

    fixed_ist = datetime(2026, 10, 1, 10, 0, 0)

    monkeypatch.setattr(
        signals,
        "now_ist_naive",
        lambda: fixed_ist,
    )

    response = signals.get_strategy_state()

    assert response["session_phase"] == "ENTRY_WINDOW"


def test_strategy_state_does_not_depend_on_server_timezone(monkeypatch):
    from backend import signals

    monkeypatch.setattr(
        signals,
        "now_ist_naive",
        lambda: datetime(2026, 10, 1, 14, 0, 0),
    )

    response = signals.get_strategy_state()

    assert response["session_phase"] == "POSITION_MANAGEMENT"
