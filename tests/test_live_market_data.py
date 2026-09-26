"""
Unit tests for Task 3: Live Market Data Pipeline integration.
Tests KiteTicker streaming, MultiSymbolCandleAggregator tick processing,
and strategy evaluation flow.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch
import pytest

from broker.kite_adapter import KiteBrokerAdapter
from config.universe import StockUniverse
from data.candle_aggregator import MultiSymbolCandleAggregator
from strategy.prediction_service import prediction_service


def test_start_and_stop_market_stream_with_mock_kiteticker():
    adapter = KiteBrokerAdapter(api_key="mock_key", access_token="mock_token")
    mock_ticker = MagicMock()

    with patch("broker.kite_adapter.KiteTicker", return_value=mock_ticker):
        kws = adapter.start_market_stream(instruments=["RELIANCE", "TCS"], mode="full")

        assert kws is not None
        assert adapter.kws == mock_ticker
        mock_ticker.connect.assert_called_once_with(threaded=True)

        # Trigger on_connect callback artificially
        adapter.kws.on_connect(mock_ticker, {})
        mock_ticker.subscribe.assert_called()
        mock_ticker.set_mode.assert_called()

        # Stop stream
        adapter.stop_market_stream()
        mock_ticker.close.assert_called_once()
        assert adapter.kws is None


def test_end_to_end_live_tick_pipeline_to_strategy_evaluation():
    universe = StockUniverse()
    test_symbols = [r.symbol for r in universe.large_cap_100[:5]]  # 5 symbols subset test

    # Token map
    token_map = {idx + 1000: sym for idx, sym in enumerate(test_symbols)}
    closed_candles = []

    def on_close_callback(candle_dict, vwap):
        closed_candles.append((candle_dict, vwap))

    multi_agg = MultiSymbolCandleAggregator(
        token_to_symbol_map=token_map,
        timeframe_minutes=15,
        on_candle_close=on_close_callback,
    )

    t1 = datetime(2026, 3, 2, 9, 15, 0)
    t2 = datetime(2026, 3, 2, 9, 29, 59)
    t3 = datetime(2026, 3, 2, 9, 30, 1)

    # Send 15m ticks for symbol 0
    sym0 = test_symbols[0]
    tok0 = 1000

    ticks = [
        {"instrument_token": tok0, "last_price": 2500.0, "last_traded_quantity": 100, "exchange_timestamp": t1},
        {"instrument_token": tok0, "last_price": 2550.0, "last_traded_quantity": 200, "exchange_timestamp": t2},
        {"instrument_token": tok0, "last_price": 2520.0, "last_traded_quantity": 50, "exchange_timestamp": t3},
    ]

    multi_agg.process_ticks(ticks)

    assert len(closed_candles) == 1
    c_dict, vwap = closed_candles[0]
    assert c_dict["symbol"] == sym0
    assert c_dict["open"] == 2500.0
    assert c_dict["high"] == 2550.0
    assert c_dict["close"] == 2550.0

    # Get completed dataframe and run strategy prediction
    df_15m = multi_agg.get_symbol_dataframe(sym0)
    assert not df_15m.empty
    preds, consensus = prediction_service.evaluate_symbol(sym0, df_15m, current_ltp=2520.0, token=tok0)

    assert "orb" in preds
    assert "cpr" in preds
    assert "dual_ema" in preds
    assert "direction" in consensus
    assert "label" in consensus
