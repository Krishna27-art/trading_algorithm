"""
Unit Tests for Intraday ORB Strategy Rules and Exits.
"""

from datetime import date, datetime, time
import pytest

from config.settings import InstrumentConfig, InstrumentType, StrategyConfig
from strategy.base_strategy import SignalAction
from strategy.orb_strategy import IntradayORBStrategy


@pytest.fixture
def strategy():
    inst = InstrumentConfig(
        symbol="NIFTY",
        exchange="NFO",
        instrument_type=InstrumentType.FUTURES,
        lot_size=25,
        min_orb_range=40.0,
        max_orb_range=120.0,
        max_risk_cap=80.0,
    )
    strat = IntradayORBStrategy(instrument=inst)
    strat.reset_session(date(2026, 3, 2))
    return strat


def test_orb_establishment_and_long_signal(strategy):
    d = date(2026, 3, 2)
    # Candle 1: 09:15-09:30
    c1 = {"datetime": datetime.combine(d, time(9, 30)), "open": 24000.0, "high": 24080.0, "low": 24000.0, "close": 24060.0}
    strategy.on_candle(c1, vwap=24030.0)

    # Candle 2: 09:30-09:45 (Establishes ORB: High=24100, Low=23975, Width=125 > 120)
    c2 = {"datetime": datetime.combine(d, time(9, 45)), "open": 24060.0, "high": 24100.0, "low": 23975.0, "close": 24070.0}
    strategy.on_candle(c2, vwap=24050.0)

    assert strategy.orb is not None
    assert strategy.orb.high == 24100.0
    assert strategy.orb.low == 23975.0
    assert strategy.orb.width == 125.0
    assert strategy.orb.is_valid_volatility is True

    # Candle 3: 09:45-10:00 Closes strictly above ORB High (24120 > 24100) and VWAP (24120 > 24060)
    c3 = {"datetime": datetime.combine(d, time(10, 0)), "open": 24070.0, "high": 24130.0, "low": 24060.0, "close": 24120.0}
    sig = strategy.on_candle(c3, vwap=24060.0)

    assert sig is not None
    assert sig.action == SignalAction.BUY
    assert sig.price == 24120.0
    assert sig.stop_loss == 23975.0 # Stop = OR_Low
    # OR_Width was 125 > 120, so effective risk is capped at 80.
    # Target = 24120 + 2.0 * 80 = 24280.
    assert sig.target == 24280.0


def test_trailing_stop_to_breakeven(strategy):
    # Setup open long position at 24100 with stop 24050 (risk = 50 pts, target = 24200)
    strategy.register_trade_entry(entry_price=24100.0, position=1, stop_loss=24050.0, target=24200.0, risk_dist=50.0)

    # Price moves to +1R (24150)
    d = datetime(2026, 3, 2, 10, 15)
    strategy.on_tick(price=24150.0, timestamp=d)

    assert strategy.trailing_breakeven_active is True
    assert strategy.stop_loss == 24100.0 # Stop moved to breakeven


def test_time_square_off_at_1430(strategy):
    strategy.register_trade_entry(entry_price=24100.0, position=1, stop_loss=24050.0, target=24200.0, risk_dist=50.0)
    
    # Tick at 14:30:00 IST
    d = datetime(2026, 3, 2, 14, 30, 0)
    sig = strategy.on_tick(price=24120.0, timestamp=d)

    assert sig is not None
    assert sig.action == SignalAction.EXIT
    assert sig.reason == "TIME_SQUARE_OFF"
