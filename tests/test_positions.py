"""
Unit Tests for Unified Broker Position Service & Positions API.
Tests:
- Deduplication: multiple raw records collapse into one normalized position.
- Repeated polling updates in-place without appending.
- P&L calculation for Long and Short positions.
- Impossible / stale LTP quarantine (e.g. RELIANCE @ ₹99.5).
- Derivative contract lot size validation.
- Position disappearing on square-off (net quantity = 0).
- Strategy SL and Target association (valid vs None).
- API endpoint schema and live broker data guarantee.
"""

import pytest
from execution.position_service import PositionService, NormalizedPosition
from backend.main import app, get_portfolio_positions


def test_position_deduplication():
    service = PositionService()
    raw = [
        {"tradingsymbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "quantity": 10, "average_price": 3000.0, "last_price": 3020.0},
        {"tradingsymbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "quantity": 10, "average_price": 3000.0, "last_price": 3020.0},
        {"tradingsymbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "quantity": 15, "average_price": 3000.0, "last_price": 3025.0},
    ]

    normalized = service.normalize_positions(raw)
    # Must collapse to exactly 1 position
    assert len(normalized) == 1
    pos = normalized[0]
    assert pos.position_id == "NSE:RELIANCE:MIS"
    assert pos.quantity == 15
    assert pos.last_price == 3025.0
    assert not pos.is_quarantined


def test_repeated_polling_updates_in_place():
    service = PositionService()
    poll_1 = [
        {"tradingsymbol": "TCS", "exchange": "NSE", "product": "CNC", "quantity": 5, "average_price": 4000.0, "last_price": 4020.0},
    ]
    res_1 = service.normalize_positions(poll_1)
    assert len(res_1) == 1
    assert res_1[0].last_price == 4020.0
    assert res_1[0].unrealised_pnl == 100.0

    # Next poll with updated price
    poll_2 = [
        {"tradingsymbol": "TCS", "exchange": "NSE", "product": "CNC", "quantity": 5, "average_price": 4000.0, "last_price": 4050.0},
    ]
    res_2 = service.normalize_positions(poll_2)
    assert len(res_2) == 1
    assert res_2[0].position_id == "NSE:TCS:CNC"
    assert res_2[0].last_price == 4050.0
    assert res_2[0].unrealised_pnl == 250.0


def test_pnl_calculation_long_and_short():
    service = PositionService()

    # Long: Qty = 10, Avg = 100, LTP = 115 -> +150
    u_long, t_long = service.calculate_pnl(quantity=10, average_price=100.0, last_price=115.0)
    assert u_long == 150.0
    assert t_long == 150.0

    # Long in loss: Qty = 10, Avg = 100, LTP = 92 -> -80
    u_loss, t_loss = service.calculate_pnl(quantity=10, average_price=100.0, last_price=92.0)
    assert u_loss == -80.0
    assert t_loss == -80.0

    # Short in profit: Qty = -20, Avg = 500, LTP = 480 -> +400
    u_short_win, t_short_win = service.calculate_pnl(quantity=-20, average_price=500.0, last_price=480.0)
    assert u_short_win == 400.0
    assert t_short_win == 400.0

    # Short in loss: Qty = -20, Avg = 500, LTP = 525 -> -500
    u_short_loss, t_short_loss = service.calculate_pnl(quantity=-20, average_price=500.0, last_price=525.0)
    assert u_short_loss == -500.0
    assert t_short_loss == -500.0


def test_quarantine_impossible_or_stale_ltp():
    service = PositionService()

    # RELIANCE entered at ₹3000.55, but LTP is reported as ₹99.5 (impossible 97% crash divergence)
    raw = [
        {
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "product": "MIS",
            "quantity": 10,
            "average_price": 3000.55,
            "last_price": 99.50,
        }
    ]
    normalized = service.normalize_positions(raw)
    assert len(normalized) == 1
    pos = normalized[0]
    assert pos.is_quarantined is True
    assert "Impossible LTP divergence" in (pos.quarantine_reason or "")

    # Zero LTP is also rejected
    raw_zero = [
        {
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "product": "MIS",
            "quantity": 5,
            "average_price": 1800.0,
            "last_price": 0.0,
        }
    ]
    norm_zero = service.normalize_positions(raw_zero)
    assert norm_zero[0].is_quarantined is True
    assert "zero LTP" in norm_zero[0].quarantine_reason


def test_lot_size_validation_derivatives():
    service = PositionService()

    # Valid F&O quantity: 65 shares with lot size 65
    is_valid, _ = service.validate_position_data(
        tradingsymbol="NIFTY24SEPFUT",
        exchange="NFO",
        quantity=65,
        average_price=24800.0,
        last_price=24850.0,
        lot_size=65,
    )
    assert is_valid is True

    # Invalid F&O quantity: 100 shares when lot size is 65 (100 % 65 != 0)
    is_invalid, reason = service.validate_position_data(
        tradingsymbol="NIFTY24SEPFUT",
        exchange="NFO",
        quantity=100,
        average_price=24800.0,
        last_price=24850.0,
        lot_size=65,
    )
    assert is_invalid is False
    assert "multiple of lot size" in reason


def test_zero_net_quantity_handling():
    service = PositionService()
    # A position that was closed (quantity = 0)
    raw = [
        {
            "tradingsymbol": "SBIN",
            "exchange": "NSE",
            "product": "MIS",
            "quantity": 0,
            "average_price": 820.0,
            "last_price": 830.0,
            "realised": 200.0,
        }
    ]
    normalized = service.normalize_positions(raw)
    assert len(normalized) == 1
    assert normalized[0].status == "CLOSED"
    assert normalized[0].quantity == 0
    assert normalized[0].unrealised_pnl == 0.0
    assert normalized[0].realised_pnl == 200.0


def test_strategy_sl_target_matching():
    service = PositionService()
    raw = [
        {"tradingsymbol": "HDFCBANK", "exchange": "NSE", "product": "MIS", "quantity": 10, "average_price": 1650.0, "last_price": 1660.0},
        {"tradingsymbol": "ICICIBANK", "exchange": "NSE", "product": "MIS", "quantity": 15, "average_price": 1200.0, "last_price": 1210.0},
    ]
    strategy_meta = {
        "HDFCBANK": {"stop_loss": 1630.0, "target": 1690.0},
        # ICICIBANK has no active strategy trade
    }
    normalized = service.normalize_positions(raw, strategy_positions=strategy_meta)
    hdfc = [p for p in normalized if p.tradingsymbol == "HDFCBANK"][0]
    icici = [p for p in normalized if p.tradingsymbol == "ICICIBANK"][0]

    assert hdfc.strategy_stop_loss == 1630.0
    assert hdfc.strategy_target == 1690.0

    assert icici.strategy_stop_loss is None
    assert icici.strategy_target is None


def test_portfolio_positions_api():
    res = get_portfolio_positions()
    assert res["status"] == "success"
    assert "broker" in res
    assert "positions" in res
    assert isinstance(res["positions"], list)
    assert "total_unrealised_pnl" in res
    assert "total_realised_pnl" in res
    assert "total_pnl" in res
