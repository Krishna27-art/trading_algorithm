import json
from datetime import datetime, timedelta

from data.instrument_resolver import (
    InstrumentResolver,
    UNIVERSE_TOKEN_CACHE_VERSION,
)


def _write_cache(path, symbols, tokens, generated_at=None):
    payload = {
        "version": UNIVERSE_TOKEN_CACHE_VERSION,
        "exchange": "NSE",
        "generated_at": (
            generated_at or datetime.now()
        ).isoformat(),
        "symbols": sorted(symbols),
        "tokens": tokens,
    }

    path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_valid_cache_is_accepted(tmp_path):
    resolver = InstrumentResolver(cache_dir=tmp_path)

    symbols = {"RELIANCE", "TCS"}
    tokens = {
        "RELIANCE": 738561,
        "TCS": 2953217,
    }

    cache = tmp_path / "universe_300_tokens.json"
    _write_cache(cache, symbols, tokens)

    result = resolver._load_valid_universe_cache(
        cache,
        symbols,
    )

    assert result == tokens


def test_stale_cache_is_rejected(tmp_path):
    resolver = InstrumentResolver(cache_dir=tmp_path)

    symbols = {"RELIANCE", "TCS"}
    tokens = {
        "RELIANCE": 738561,
        "TCS": 2953217,
    }

    cache = tmp_path / "universe_300_tokens.json"

    old_time = datetime.now() - timedelta(hours=25)

    _write_cache(
        cache,
        symbols,
        tokens,
        generated_at=old_time,
    )

    result = resolver._load_valid_universe_cache(
        cache,
        symbols,
    )

    assert result is None


def test_partial_cache_is_rejected(tmp_path):
    resolver = InstrumentResolver(cache_dir=tmp_path)

    target_symbols = {
        "RELIANCE",
        "TCS",
        "HDFCBANK",
    }

    cache = tmp_path / "universe_300_tokens.json"

    _write_cache(
        cache,
        target_symbols - {"HDFCBANK"},
        {
            "RELIANCE": 738561,
            "TCS": 2953217,
        },
    )

    result = resolver._load_valid_universe_cache(
        cache,
        target_symbols,
    )

    assert result is None


def test_duplicate_tokens_in_cache_are_rejected(tmp_path):
    resolver = InstrumentResolver(cache_dir=tmp_path)

    symbols = {
        "RELIANCE",
        "TCS",
    }

    cache = tmp_path / "universe_300_tokens.json"

    _write_cache(
        cache,
        symbols,
        {
            "RELIANCE": 12345,
            "TCS": 12345,
        },
    )

    result = resolver._load_valid_universe_cache(
        cache,
        symbols,
    )

    assert result is None


def test_legacy_plain_dict_cache_is_rejected(tmp_path):
    resolver = InstrumentResolver(cache_dir=tmp_path)

    symbols = {
        "RELIANCE",
        "TCS",
    }

    cache = tmp_path / "universe_300_tokens.json"

    cache.write_text(
        json.dumps(
            {
                "RELIANCE": 738561,
                "TCS": 2953217,
            }
        ),
        encoding="utf-8",
    )

    result = resolver._load_valid_universe_cache(
        cache,
        symbols,
    )

    assert result is None


def test_duplicate_tokens_from_kite_are_not_cached(tmp_path):
    resolver = InstrumentResolver(cache_dir=tmp_path)

    class FakeKite:
        def instruments(self, exchange):
            return [
                {
                    "tradingsymbol": "RELIANCE",
                    "instrument_token": 12345,
                },
                {
                    "tradingsymbol": "TCS",
                    "instrument_token": 12345,
                },
            ]

    cache = tmp_path / "universe_300_tokens.json"

    resolved, unresolved = resolver.resolve_universe(
        symbols=["RELIANCE", "TCS"],
        kite_client=FakeKite(),
        cache_path=cache,
        force_refresh=True,
    )

    assert resolved == {}
    assert set(unresolved) == {"RELIANCE", "TCS"}
    assert not cache.exists()
