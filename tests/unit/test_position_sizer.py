"""
Unit tests for Position Sizer: Verifying true economic stop risk enforcement.
"""

import pytest
from config.settings import InstrumentConfig, InstrumentType, RiskConfig
from risk.position_sizer import PositionSizer


def test_position_sizer_true_economic_risk_invariant():
    """
    Verifies that for stops: 10, 40, 80, 81, 120, 150, 300:
    risk_taken = quantity * actual_stop_distance * point_value
    and risk_taken <= configured risk budget.
    """
    capital = 1_000_000.0
    risk_pct = 0.01  # 1% = 10,000 budget
    risk_cfg = RiskConfig(initial_capital=capital, risk_per_trade_pct=risk_pct)
    sizer = PositionSizer(risk_cfg)

    # NIFTY Futures, lot_size=25
    inst_futures = InstrumentConfig(
        symbol="NIFTY",
        exchange="NFO",
        instrument_type=InstrumentType.FUTURES,
        lot_size=25,
        min_orb_range=40.0,
        max_orb_range=120.0,
        max_risk_cap=80.0,
    )

    test_stops = [10.0, 40.0, 80.0, 81.0, 120.0, 150.0, 300.0]
    risk_budget = capital * risk_pct

    for stop_dist in test_stops:
        qty = sizer.calculate_order_quantity(
            capital=capital,
            stop_distance=stop_dist,
            instrument=inst_futures,
        )
        risk_taken = qty * stop_dist  # point value = 1.0 per unit in Nifty
        assert risk_taken <= risk_budget, f"Risk taken ₹{risk_taken} exceeded budget ₹{risk_budget} for stop {stop_dist}"
        if qty > 0:
            assert qty % inst_futures.lot_size == 0, f"Quantity {qty} not a multiple of lot size 25"

    # Also verify for Equity (lot_size=1)
    inst_equity = InstrumentConfig(
        symbol="RELIANCE",
        exchange="NSE",
        instrument_type=InstrumentType.EQUITY,
        lot_size=1,
    )

    for stop_dist in test_stops:
        qty = sizer.calculate_order_quantity(
            capital=capital,
            stop_distance=stop_dist,
            instrument=inst_equity,
        )
        risk_taken = qty * stop_dist
        assert risk_taken <= risk_budget, f"Equity risk taken ₹{risk_taken} exceeded budget ₹{risk_budget} for stop {stop_dist}"


def test_rejection_when_enforce_max_risk_cap_enabled():
    capital = 1_000_000.0
    risk_cfg = RiskConfig(initial_capital=capital, risk_per_trade_pct=0.01)
    sizer = PositionSizer(risk_cfg)

    inst = InstrumentConfig(
        symbol="NIFTY",
        exchange="NFO",
        instrument_type=InstrumentType.FUTURES,
        lot_size=25,
        max_risk_cap=80.0,
    )

    # Stop 100 > max_risk_cap 80 -> rejected when enforce_max_risk_cap=True
    qty = sizer.calculate_order_quantity(
        capital=capital,
        stop_distance=100.0,
        instrument=inst,
        enforce_max_risk_cap=True,
    )
    assert qty == 0
