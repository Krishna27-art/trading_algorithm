"""
Unit Tests for October 2024 Revised SEBI Indian Market Transaction Costs.
"""

from config.settings import InstrumentType, TransactionCostConfig
from risk.transaction_costs import TransactionCostCalculator


def test_futures_transaction_costs():
    cost_calc = TransactionCostCalculator()

    # Trade 1 lot of NIFTY (25 units) bought at 24000 and sold at 24100
    # Buy turnover = 24000 * 25 = 6,00,000
    # Sell turnover = 24100 * 25 = 6,02,500
    # Aggregate turnover = 12,02,500
    breakdown = cost_calc.calculate_trade_costs(
        symbol="NIFTY",
        instrument_type=InstrumentType.FUTURES,
        buy_price=24000.0,
        sell_price=24100.0,
        quantity=25,
    )

    # 1. Brokerage = ₹20 * 2 = ₹40.00
    assert breakdown.brokerage == 40.0

    # 2. STT = 0.02% on sell turnover (6,02,500 * 0.0002) = 120.50
    assert breakdown.stt == 120.50

    # 3. Exchange Charges = 0.0019% on 12,02,500 = 22.85
    assert breakdown.exchange_charges == 22.85

    # 4. Stamp duty = 0.002% on 6,00,000 = 12.00
    assert breakdown.stamp_duty == 12.00

    # 5. Slippage = 0.50 pts * 25 = 12.50
    assert breakdown.slippage_cost == 12.50

    # Total cost should include GST (18% on brokerage + exchange + sebi)
    assert breakdown.total_cost > 200.0
