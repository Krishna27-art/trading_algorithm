"""
Unit tests for SSFOneMinuteRuntime real-token resolution and safety.
"""

import pytest
from backend.streaming.ssf_runtime import SSFOneMinuteRuntime


def test_ssf_runtime_never_creates_fake_tokens(monkeypatch):
    runtime = SSFOneMinuteRuntime()

    class FakeAggregator:
        def __init__(self, **kwargs):
            self.token_to_symbol_map = kwargs["token_to_symbol_map"]

    monkeypatch.setattr(
        "backend.streaming.ssf_runtime.MultiSymbolCandleAggregator",
        FakeAggregator,
    )

    class FakeResolver:
        @staticmethod
        def resolve_token(sym, exchange, kite_client):
            if sym == "GOOD":
                return 123456
            return None

    monkeypatch.setattr(
        "backend.streaming.ssf_runtime.instrument_resolver",
        FakeResolver,
    )

    runtime.initialize(
        symbols=["GOOD", "BAD"],
        kite_client=object(),
        seed_history=False,
    )

    assert runtime._aggregator.token_to_symbol_map == {123456: "GOOD"}
    assert all(token > 0 for token in runtime._aggregator.token_to_symbol_map)
    assert "BAD" not in runtime._aggregator.token_to_symbol_map.values()


def test_ssf_runtime_raises_when_no_tokens_resolved(monkeypatch):
    runtime = SSFOneMinuteRuntime()

    class FakeResolver:
        @staticmethod
        def resolve_token(sym, exchange, kite_client):
            return None

    monkeypatch.setattr(
        "backend.streaming.ssf_runtime.instrument_resolver",
        FakeResolver,
    )

    with pytest.raises(RuntimeError) as exc:
        runtime.initialize(
            symbols=["BAD1", "BAD2"],
            kite_client=object(),
            seed_history=False,
        )
    assert "no valid real Kite instrument tokens" in str(exc.value)
