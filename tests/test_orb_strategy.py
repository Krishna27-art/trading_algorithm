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
        max_risk_cap=150.0,
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
    # Risk distance is 24120 - 23975 = 145 pts <= max_risk_cap (150).
    # Target = 24120 + 2.0 * 145 = 24410.
    assert sig.target == 24410.0


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


def test_equity_orb_rejects_trade_when_actual_stop_risk_exceeds_cap():
    inst = InstrumentConfig(
        symbol="TESTEQ",
        exchange="NSE",
        instrument_type=InstrumentType.EQUITY,
        lot_size=1,
        tick_size=0.05,
        min_orb_range=1.0,
        max_orb_range=100.0,
        max_risk_cap=80.0,
        equity_orb_max_risk_pct=0.004,  # 0.40% explicitly configured
    )

    strategy = IntradayORBStrategy(instrument=inst)
    strategy.reset_session(date(2026, 3, 2))

    c1 = {
        "datetime": datetime(2026, 3, 2, 9, 15),
        "open": 5000.0,
        "high": 5040.0,
        "low": 4990.0,
        "close": 5035.0,
    }

    c2 = {
        "datetime": datetime(2026, 3, 2, 9, 30),
        "open": 5035.0,
        "high": 5055.0,
        "low": 4990.0,
        "close": 5045.0,
    }

    strategy.on_candle(c1, vwap=5020.0)
    strategy.on_candle(c2, vwap=5035.0)

    breakout = {
        "datetime": datetime(2026, 3, 2, 9, 45),
        "open": 5045.0,
        "high": 5060.0,
        "low": 5040.0,
        "close": 5055.0,
    }

    sig = strategy.on_candle(breakout, vwap=5040.0)

    # Entry 5055 - ORB low 4990 = 65.
    # 0.40% of 5055 = 20.22.
    # Actual stop risk is too large, so no trade is allowed.
    assert sig is None


def test_equity_orb_target_uses_actual_accepted_risk():
    from indicators.orb import OpeningRange

    inst = InstrumentConfig(
        symbol="TESTEQ",
        exchange="NSE",
        instrument_type=InstrumentType.EQUITY,
        lot_size=1,
        tick_size=0.05,
        min_orb_range=1.0,
        max_orb_range=100.0,
        max_risk_cap=80.0,
        equity_orb_max_risk_pct=0.02,
    )

    strategy = IntradayORBStrategy(instrument=inst)
    strategy.reset_session(date(2026, 3, 2))

    strategy.orb = OpeningRange(
        high=1010.0,
        low=1000.0,
        width=10.0,
        is_valid_volatility=True,
    )

    candle = {
        "datetime": datetime(2026, 3, 2, 10, 0),
        "open": 1010.0,
        "high": 1025.0,
        "low": 1008.0,
        "close": 1015.0,
    }

    sig = strategy._entry_signal(candle, vwap=1005.0)

    assert sig is not None
    assert sig.stop_loss == 1000.0

    # Actual risk = 1015 - 1000 = 15
    # Target = 1015 + 2 * 15 = 1045
    assert sig.target == 1045.0

