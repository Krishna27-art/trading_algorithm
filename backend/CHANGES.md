# backend.zip — changes

Scope: the ZIP you sent contains only `backend/` (7 API files). `streaming/`, `strategy/`,
`broker/`, `data/`, `scanner/`, `config/` were NOT included, so nothing in them was read or
changed. Every fix below is in `backend/`. Tests stub those missing modules.

## Files
| File | Change |
|---|---|
| `main.py` | History warmer no longer starts at import time (opt-in `ENABLE_LEGACY_HISTORY_WARMER=1`, runs in app startup). CORS methods/headers restricted to GET/POST/OPTIONS + Content-Type/X-Shared-Secret; origins from `CORS_ORIGINS` (wildcard ignored). Backtest router mounted separately (`ENABLE_BACKTEST_ROUTES`, default on). |
| `security.py` | Fixed: non-ASCII `X-Shared-Secret` made `hmac.compare_digest` raise TypeError -> HTTP 500. Now compares UTF-8 bytes -> 401. |
| `kite.py` | Removed `/api/profile`, `/api/margins`, duplicate `/api/logout` (keep `/kite/logout`). No invented product/exchange defaults. `profile()` failure now logged. |
| `market.py` | Missing prev_close/open/vwap are `None` (were silently = LTP). Failed quote batches are explicit: per-symbol `DATA_UNAVAILABLE` + `reason`, response `PARTIAL`/`DATA_UNAVAILABLE`, `failed_batches`. Per-stock timestamp is the exchange timestamp (was server "now"); status `LIVE`/`STALE`. |
| `system.py` | No hardcoded `RUNNING`/`READY`. `strategy_engine`, `overall_status` (LIVE/STALE/DISCONNECTED/ERROR/STANDBY) derived from Kite session, stream state, signal engine. `risk_engine` = `NOT_APPLICABLE`. |
| `stream_routes.py` | Removed client-supplied `tokens` override on `/api/stream/start` (fake-token vector). `/api/stream/signals` reads the engine once (was twice). Responses carry `stream_state`. 500s no longer leak exception text. |
| `signals.py` | Removed `/api/research/live`, `/api/strategy/trades`, `/api/risk/summary`. Telemetry hardened (below). Scanner: no invented `700` default, no exception text leaked. `/api/strategy/state`: unknown strategy -> 400 (was silently CPR); trading-risk fields dropped. |
| `backtest_routes.py` (new) | Backtests isolated from live path, secret-protected, one-at-a-time (429). Removes ~200 duplicated lines. `token or 0` removed; unknown strategy -> 400 (was silent ORB); shared settings deep-copied. |

### Telemetry (`/api/strategy/telemetry`)
- Candle with missing/malformed timestamp is dropped (was stamped "now"); count in `dropped_candles`.
- Forming 15m candle excluded from strategy input (still on chart, `complete:false`).
- Strategies run only when a completed bar of the CURRENT session exists (previous-session bars are chart-only).
- Requires a real quote with positive LTP; stale/timestamp-less quote during market hours -> `STALE`, no signal.
- Per-candle VWAP = real cumulative session VWAP of that candle's own day (was today's quote VWAP stamped on every historical candle).
- No ORB fallback when the requested strategy has no prediction. Exception detail not returned to client.
- Hardcoded `max_risk_cap = 80.0` / `ltp*0.004` removed; shared `settings.instruments[0]` no longer mutated.
- 20s TTL cache on Kite `historical_data` (AOU pulls 25 days per call).
- New `data_state`: LIVE / SESSION_CLOSED / STALE / NO_CURRENT_SESSION / DATA_UNAVAILABLE / TOKEN_UNRESOLVED / AUTH_REQUIRED / ERROR.

## Contract changes the frontend must tolerate
- Removed routes: `/api/research/live`, `/api/strategy/trades`, `/api/risk/summary`, `/api/profile`, `/api/margins`, `/api/logout`. (I could not search the frontend; `kite.py`'s own docstring says it only uses `/kite/login`, `/kite/status`, `/kite/logout`.)
- Telemetry: unavailable numbers are `null` (were `0.0`); `active_trade` and `risk_summary` are `null`; `active_signal` no longer has `risk_approved`/`risk_rejection_reason`; `active_signal.entry` is `null` if the strategy gave none (was LTP).
- `/api/market/prices`: `status` can be `PARTIAL`; per-stock status can be `STALE`; nullable fields.
- Backtest routes now need `X-Shared-Secret`.

## NOT done (files not in the ZIP) — still open
`market_stream_manager.py` (sync evaluation in tick callback, partial first candle, tick->engine propagation, cumulative volume, restart generation), `live_signal_engine.py`, `ssf_one_minute_runtime.py` (`hash()` token), `live_market_state.py`, `aou_oss_strategy.py` / `prediction_service.py` (latency, replay), `kite_adapter.py` (second KiteTicker, positions/orders/margins, `datetime.now()`, session-file perms), `sector_peer_manager.py`, `instrument_resolver.py`, `history_context_warmer.py`, DB trade state, `risk/*`.
Send those and I'll do them. No stream-callback, partial-candle, reconnect, volume, AOU-latency or 300-symbol-throughput tests were run — nothing to run them against.

## Not fully resolved inside backend/
- `/api/strategy/telemetry` and `/api/strategy/scanner` still use REST polling, not the canonical stream state (`live_market_state` / `live_signal_engine` APIs weren't visible). Kept for the workstation, flagged as a remaining violation.
- `/api/strategy/state` still echoes hardcoded schedule strings and `lot_size`; `/api/strategy/scanner` still has static `scoring_weights`. Can't verify against settings/scanner.
- Verify `PredictionService` doesn't read `settings.instruments[0].max_risk_cap` (telemetry no longer sets it).
- `system.py` assumes the stream `state` strings (`LIVE`/`CONNECTED`/`RUNNING`, `*STALE*`, `*ERROR*`); adjust `_LIVE_STATES` to your enum.
- Backtest cached CSVs are reused with no expiry (pre-existing).

## Tests
`python -m pytest tests` -> 60 passed. Same suite against your ORIGINAL backend: 51 failed / 9 passed (the 9 cover behavior that was already correct), confirming the tests detect the bugs. Tests run the real `backend/*.py` against stubs of the missing modules; they are not integration tests against Kite.
