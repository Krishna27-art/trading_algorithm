"""
Unit tests for data.candle_aggregator (Candle and CandleAggregator).
"""

from datetime import datetime, timedelta
import pytest
from data.candle_aggregator import Candle, CandleAggregator


def test_candle_update_and_dict():
    t0 = datetime(2026, 3, 2, 9, 15, 0)
    candle = Candle(symbol="NIFTY", start_time=t0, timeframe_minutes=15)
    
    assert candle.open is None
    candle.update(price=24000.0, volume=10)
    assert candle.open == 24000.0
    assert candle.high == 24000.0
    assert candle.low == 24000.0
    assert candle.close == 24000.0
    assert candle.volume == 10

    candle.update(price=24050.0, volume=20)
    assert candle.high == 24050.0
    assert candle.close == 24050.0
    assert candle.volume == 30

    candle.update(price=23980.0, volume=15)
    assert candle.low == 23980.0
    assert candle.close == 23980.0
    assert candle.volume == 45

    d = candle.to_dict()
    assert d["symbol"] == "NIFTY"
    assert d["datetime"] == t0
    assert d["open"] == 24000.0
    assert d["high"] == 24050.0
    assert d["low"] == 23980.0
    assert d["close"] == 23980.0
    assert d["volume"] == 45


def test_candle_aggregator_ticks_and_closure():
    closed_candles = []
    closed_vwaps = []

    def on_close(candle_dict, vwap):
        closed_candles.append(candle_dict)
        closed_vwaps.append(vwap)

    aggregator = CandleAggregator(
        symbol="NIFTY",
        timeframe_minutes=15,
        on_candle_close=on_close,
    )

    t1 = datetime(2026, 3, 2, 9, 15, 10)
    t2 = datetime(2026, 3, 2, 9, 20, 0)
    t3 = datetime(2026, 3, 2, 9, 29, 59)
    # Next candle trigger
    t4 = datetime(2026, 3, 2, 9, 30, 1)

    aggregator.process_tick(price=24000.0, volume=100, timestamp=t1)
    aggregator.process_tick(price=24050.0, volume=200, timestamp=t2)
    aggregator.process_tick(price=24020.0, volume=100, timestamp=t3)

    assert len(closed_candles) == 0

    # Tick at 09:30:01 closes the 09:15 candle and starts 09:30 candle
    aggregator.process_tick(price=24060.0, volume=100, timestamp=t4)

    assert len(closed_candles) == 1
    c1 = closed_candles[0]
    assert c1["datetime"] == datetime(2026, 3, 2, 9, 15, 0)
    assert c1["open"] == 24000.0
    assert c1["high"] == 24050.0
    assert c1["low"] == 24000.0
    assert c1["close"] == 24020.0
    assert c1["volume"] == 400

    # Total volume: 100*24000 + 200*24050 + 100*24020 + 100*24060 = 12018000 / 500 = 24036.0
    assert closed_vwaps[0] == pytest.approx(24036.0)

    # Check dataframe
    df = aggregator.get_completed_dataframe()
    assert len(df) == 1
    assert "close" in df.columns

    # Test reset
    aggregator.reset_daily_session()
    assert aggregator.current_candle is None
    assert len(aggregator.completed_candles) == 0
    assert aggregator.current_vwap == 0.0


def test_multisymbol_candle_aggregator_kite_ticks():
    from data.candle_aggregator import MultiSymbolCandleAggregator

    token_map = {738561: "RELIANCE", 2953217: "TCS"}
    closed_events = []

    def on_close(candle_dict, vwap):
        closed_events.append((candle_dict, vwap))

    multi_agg = MultiSymbolCandleAggregator(
        token_to_symbol_map=token_map,
        timeframe_minutes=15,
        on_candle_close=on_close,
    )

    t1 = datetime(2026, 3, 2, 9, 15, 10)
    t2 = datetime(2026, 3, 2, 9, 20, 0)
    t3 = datetime(2026, 3, 2, 9, 30, 1)

    raw_ticks = [
        {
            "instrument_token": 738561,
            "last_price": 2950.0,
            "last_traded_quantity": 50,
            "exchange_timestamp": t1,
        },
        {
            "instrument_token": 2953217,
            "last_price": 4100.0,
            "last_traded_quantity": 25,
            "exchange_timestamp": t1,
        },
        {
            "instrument_token": 738561,
            "last_price": 2965.0,
            "last_traded_quantity": 30,
            "exchange_timestamp": t2,
        },
    ]

    multi_agg.process_ticks(raw_ticks)
    assert len(closed_events) == 0

    # Rollover tick for RELIANCE
    rollover_ticks = [
        {
            "instrument_token": 738561,
            "last_price": 2970.0,
            "last_traded_quantity": 10,
            "exchange_timestamp": t3,
        }
    ]
    multi_agg.process_ticks(rollover_ticks)

    assert len(closed_events) == 1
    rel_candle, rel_vwap = closed_events[0]
    assert rel_candle["symbol"] == "RELIANCE"
    assert rel_candle["open"] == 2950.0
    assert rel_candle["high"] == 2965.0
    assert rel_candle["low"] == 2950.0
    assert rel_candle["close"] == 2965.0
    assert rel_candle["volume"] == 80

    rel_df = multi_agg.get_symbol_dataframe("RELIANCE")
    assert len(rel_df) == 1
    assert rel_df.iloc[0]["close"] == 2965.0
