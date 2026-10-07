"""
Streaming package for real-time market data ingestion and live signal generation.
"""

from backend.streaming.live_market_state import LiveMarketState, live_market_state
from backend.streaming.live_signal_engine import LiveSignalEngine, live_signal_engine
from backend.streaming.market_stream_manager import (
    MarketStreamManager,
    StreamState,
    market_stream_manager,
)
from backend.streaming.ssf_runtime import (
    SSFContextStore,
    SSFLiveRuntime,
    SSFMarketContext,
    SSFOneMinuteRuntime,
    SSFReturnTracker,
    ssf_context_store,
    ssf_live_runtime,
    ssf_one_minute_runtime,
    ssf_return_tracker,
)

__all__ = [
    "LiveMarketState",
    "live_market_state",
    "LiveSignalEngine",
    "live_signal_engine",
    "MarketStreamManager",
    "StreamState",
    "market_stream_manager",
    "SSFLiveRuntime",
    "SSFContextStore",
    "SSFMarketContext",
    "SSFOneMinuteRuntime",
    "SSFReturnTracker",
    "ssf_context_store",
    "ssf_live_runtime",
    "ssf_one_minute_runtime",
    "ssf_return_tracker",
]

