# Full-Scale Red-Team Audit — `trading_algorithm`

**Audit date:** 9 October 2026  
**Repository:** [Krishna27-art/trading_algorithm](https://github.com/Krishna27-art/trading_algorithm)  
**Audited branch:** `main`  
**Source commit:** [`c6a2a58a1937c97ed711ff259f97421b1170d977`](https://github.com/Krishna27-art/trading_algorithm/commit/c6a2a58a1937c97ed711ff259f97421b1170d977)  
**Mode:** Read-only static audit. No repository code was modified.

**Continuation status (same source commit):** An additional targeted source trace added findings RT-25–RT-30. The report remains **INCOMPLETE**: unreviewed files are not certified clean, no local tests/build or live Kite run occurred, and this session did not modify production code.

## Executive verdict

**Do not yet trust this system to display an actionable live signal without independent verification.** The principal blocker is that a previously generated directional result can remain visible after its candle/result freshness expires: the backend refreshes freshness metadata, but does not withdraw the direction or recompute consensus, and the frontend does not gate a strategy result on that freshness. This directly conflicts with the requirement that stale data must not look like a current signal.

The most consequential research defect is separate: the default `NIFTY` backtest path configures the instrument as an NFO future but resolves the token through the NSE index alias, then fetches history using that cash-index token. The backtest can therefore combine cash-index candles with futures contract/cost assumptions.

Other high-priority findings concern missing-depth liquidity acceptance, non-fatal universe validation, API secret handling, incomplete volume-profile data being marked usable, and stream startup state/cleanup. The report also identifies deployment-dependent security exposure, calendar coverage limits, cache identity checks, and reproducibility issues.

### Snapshot and verification limits

The GitHub tree contained **1,187 tracked files**. Of these, **142 were source/configuration/documentation/test files**, while a further **1,040 were under the tracked backend cache tree**. The audit traced the main production path in detail and inspected the surrounding configuration, indicators, routes, tests, and UI. The cache metadata corpus was inventoried as a generated-data group rather than treating every repeated metadata file as independent business logic.

This is a static source audit. It did **not** execute the full test suite, connect to a live Kite account, replay live exchange ticks, run a 700-symbol market-open stress test, or inspect the contents of the tracked SQLite database binary. Findings that depend on public deployment or a particular cache state are marked conditional. The README’s “tests passing” statement was not independently verified in this audit.

---

# A. System understanding

The actual project is a read-only market-data and signal dashboard. The documented intended contract is: Kite market data in, independently computed strategy results and consensus out, the user manually decides whether to trade. The active API modules do not expose an obvious order-placement workflow in the inspected path; the presence of historical backtest and journal classes should not be interpreted as proof of live execution.

The system is not one linear pipeline. It has at least three connected but distinct flows:

1. **Live signal flow:** Kite session → `MarketStreamManager`/KiteTicker → `MultiSymbolCandleAggregator` → live market state and historical warm-up → evaluation queue → `LiveSignalEngine` → `PredictionService` → SQLite signal journal → `/api/stream/*` → React polling UI.
2. **On-demand scanner flow:** active Kite REST client → 700-stock universe and token resolution → batched quote requests → cached daily context → liquidity filter/ranking → `/api/strategy/scanner`.
3. **Research/backtest flow:** API request → instrument/token resolution → CSV cache or Kite historical fetch → backtester → report serialization.

## B. Actual architecture and C. expected architecture

**Expected:** Kite auth → validated instrument master/universe → fresh historical and streaming data → complete/valid candles → scanner/eligibility checks → isolated strategies → signal validation/freshness → consensus → API → frontend → manual user action.

**Actual:** The major components exist, but the scanner is a separate request-driven ranking subsystem; the streaming path subscribes to the resolved universe and evaluates history-ready symbols directly. The frontend reads the live signal API using timed HTTP polling (`usePolling` at 3- and 5-second intervals); the WebSocket in the core system is KiteTicker’s broker feed, not a frontend WebSocket subscription. Signal persistence, quote presentation, ranking and backtest reports also have separate storage/data paths.

The most important mismatch is not the choice of polling. It is the serving contract: fresh market ticks are used to refresh displayed LTP while old strategy predictions and the consensus can remain directional. Additional mismatches are detailed below.

## D. Architecture differences

| Area | Expected contract | Current implementation and consequence |
|---|---|---|
| Signal freshness | An expired result cannot remain an actionable-looking direction | Freshness metadata is recalculated on retrieval, but old direction/status and consensus are retained; the frontend ignores that metadata |
| NIFTY backtest input | A tradeable futures contract and matching metadata/cost assumptions | Default NIFTY path resolves the NSE cash-index token while keeping a FUTURES/NFO config |
| Universe validation | Wrong count, duplicates or malformed records abort startup | Several universe validation issues are logged, then construction proceeds with the reduced dataset |
| Liquidity gate | Spread is proven with live bid/ask data | `depth=None` skips spread validation instead of producing `DATA_UNAVAILABLE` |
| Volume profile | Profile marked usable only after full-session observation without gaps | A profile started mid-session can be saved as `COMPLETE`; previous-session loading accepts valid-looking levels even if snapshot status is degraded |
| Frontend start response | “Started” only after connection state is confirmed | Start route returns `started` while the manager may only be `CONNECTING` |
| Configuration | Declared settings have known consumers and validated defaults | Several `.env.example` variables are ignored by Pydantic settings; a weak shared-secret placeholder is supplied |
| Calendar | NSE sessions/holidays match the evaluated date | Holiday data stops at 2026; an unknown year is treated as weekdays-only |
| Runtime lifecycle | Worker threads exit cleanly on shutdown | Several background loops use `while True` and have no stop event/join path |

---

# E. Findings — actual code, failure mechanism and fixes

Severity labels: **CRITICAL** = can cause dangerously misleading live output or fundamentally invalid research; **HIGH** = serious data/reliability/security defect; **MEDIUM** = material correctness, deployment or reproducibility weakness; **LOW** = maintenance/readability issue.

## RT-01 — CRITICAL: stale directional strategies can remain visible as live signals

**Files/functions**
- [`backend/streaming/live_signal_engine.py` — freshness refresh and `get_all_predictions`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/live_signal_engine.py#L300-L365) and [`get_all_predictions`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/live_signal_engine.py#L1620-L1645)
- [`backend/routes/stream.py` — `stream_signals`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/routes/stream.py#L125-L174)
- [`frontend/src/pages/LiveSignalsPage.jsx`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/frontend/src/pages/LiveSignalsPage.jsx#L165-L215) and [`visible` filter](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/frontend/src/pages/LiveSignalsPage.jsx#L195-L210)

**Problem and evidence:** `_with_current_freshness()` updates the outer payload and per-strategy `data_freshness` blocks. It does not convert old directional predictions to `UNAVAILABLE`, nor does it recompute consensus after withdrawing stale strategy votes. `get_all_predictions()` returns that refreshed metadata with the old result objects. The stream route filters symbols by the freshness of their current quote/tick, then returns their old payload. The frontend takes `signal.predictions`/`signal.consensus` and renders their status/direction; it checks freshness to decide whether to show a current LTP, but not to suppress a stale direction.

**Failure scenario:** A valid candle-close evaluation creates `LONG`. Ticks continue to arrive, so the symbol remains feed-fresh, but the strategy-result freshness window expires or the next evaluation is delayed/dropped. The API serves the current LTP alongside the prior `LONG`; the old consensus can remain bullish. The UI labels the feed as live and displays the direction. This makes “fresh quote” look equivalent to “fresh signal”, which is false.

**Impact:** A user may manually act on a signal derived from an old completed candle or an evaluation that did not complete for the latest candle. This violates the no-stale-data contract.

**Fix:** Make result validity a serving-time invariant. At serve time, use `PredictionService.revalidate_cached()` (or a single equivalent canonical helper) to withhold stale directional results as `UNAVAILABLE`; recompute consensus using only currently valid strategy outputs; include signal/candle ages in the canonical response; make the frontend render a direction only when that strategy result is current and valid. Add regression tests where LTP continues updating while strategy evaluation is paused or the source candle exceeds the permitted age.

## RT-02 — CRITICAL: default NIFTY backtest uses the cash-index token with a futures instrument config

**Files/functions**
- [`backend/routes/backtest.py` — `_resolve_instrument`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/routes/backtest.py#L105-L135) and [`_load_history`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/routes/backtest.py#L135-L184)
- [`backend/data/instrument_resolver.py` — `CANONICAL_INDEX_TOKENS` and `resolve_token`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/data/instrument_resolver.py#L35-L61) and [`resolve_token`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/data/instrument_resolver.py#L190-L238)
- [`backend/config/settings.py` — default instrument](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/config/settings.py#L10-L32)

**Problem and evidence:** For the default `symbol == "NIFTY"` branch, `_resolve_instrument()` deep-copies `settings.instruments[0]`, which is configured as `exchange="NFO"` and `InstrumentType.FUTURES`. It then resolves `NIFTY` through `exchange="NSE"`. The resolver’s canonical alias returns the NIFTY 50 cash-index token, but `_load_history()` uses that token with the backtest’s FUTURES/NFO instrument config. The resulting candle series is not a NIFTY futures contract, while the configuration and cost code treat it as futures.

**Failure scenario:** A backtest report looks like a tradable futures result but derives entries/exits from cash-index bars and applies futures assumptions. It will not faithfully represent actual futures prices, basis, contract rollover, or costs.

**Fix:** Separate `NIFTY 50` cash index from dated NIFTY futures explicitly. For futures backtests, resolve an actual FUT instrument from the NFO master (symbol, expiry, token and lot size), validate contract continuity/roll rules, and use those same contract prices/cost assumptions. If intentionally backtesting the index, use an index-specific config and label results as a non-tradable benchmark; do not return the current `REAL_KITE` futures-like report for it. Invalidate existing NIFTY backtest caches built from the wrong token.

## RT-03 — HIGH: universe corruption is logged, not rejected

**File/function:** [`backend/config/universe.py` — `StockUniverse._load_and_validate`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/config/universe.py#L95-L190); [`resolve_700_universe_tokens`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/config/universe.py#L250-L320); [`MarketStreamManager.start_stream`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/market_stream_manager.py#L1030-L1145).

**Problem:** Duplicate symbols and malformed fields are collected and logged, and category/total count mismatches are warnings followed by “Proceeding with available stocks.” The stream startup’s `expected_symbols` is then loaded from that same potentially incomplete `StockUniverse`. The count check compares the resolved map with the corrupted list, not with a separate, fixed 700/100/100/500 contract.

**Failure scenario:** If the master JSON loses a symbol or category rows, tokens for every remaining record resolve successfully; both the “expected” and “resolved” sets agree and the stream can start with fewer than 700 stocks. Bad rank/category/name values can also enter ranking logic after an error is logged.

**Fix:** Validate field types, unique symbol and token mappings, exact total and category counts. Raise a clear exception on any required-universe defect before stream startup. Compare against explicit required counts (and a checked-in/versioned master checksum or expected membership metadata), not the length of the same file being validated. Add tests that delete one row, duplicate a symbol, corrupt a category and provide a non-integer rank; each must prevent stream startup.

## RT-04 — HIGH: missing order-book depth can pass the scanner liquidity gate

**File/function:** [`backend/scanner/liquidity_filter.py` — `_best_prices` and `evaluate_stock`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/scanner/liquidity_filter.py#L70-L190).

**Problem:** The function validates spread when `depth_data is not None`, but a `None` depth does not add a failure. Execution proceeds to other checks and can return `PASS` even though bid/ask spread was never measured.

**Failure scenario:** Kite’s quote response lacks depth or a payload has no buy/sell levels. The candidate can pass on price/volume/ADTV alone, despite the required spread gate being unproven.

**Fix:** Treat absent, empty, malformed, crossed or stale depth as `DATA_UNAVAILABLE`/reject. Separate “not enough evidence to calculate spread” from a valid spread pass. Add tests for `None`, missing keys, empty levels, crossed bid/ask, malformed numbers and valid depth.

## RT-05 — HIGH: the frontend-delivered shared secret is not a server secret

**Files/functions:** [`.env.example`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/.env.example#L10-L18); [`backend/config/settings.py`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/config/settings.py#L290-L335); [`backend/security.py`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/security.py#L12-L32); [`frontend/src/api/client.js`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/frontend/src/api/client.js#L1-L50).

**Problem:** `.env.example` supplies a known non-empty placeholder `APP_SHARED_SECRET='CHANGE_ME_GENERATE_A_REAL_SECRET'`. The application settings do not fail startup when that value remains unchanged; enforcement occurs only when the dependency is used. The frontend client reads `VITE_APP_SHARED_SECRET`, which is bundled into browser-delivered JavaScript. Anything prefixed `VITE_` must be treated as public, not as a private API credential.

**Failure scenario:** A developer copies the example file and runs the app with the known secret, or deploys the frontend with `VITE_APP_SHARED_SECRET`. The secret can be extracted from static assets/devtools and used to invoke the protected stream controls, logout or research endpoints. If the service is internet-accessible, the boundary is not trustworthy.

**Fix:** Never ship a shared server secret to a browser. For local-only use, bind the backend to loopback and keep administrative controls local. For remote use, implement proper user/session authentication and authorization, secure cookies or short-lived server-verified credentials, CSRF protection where relevant, and network-level restrictions. Reject known placeholder secrets at startup. Keep all secrets out of frontend bundles and logs.

## RT-06 — HIGH: a mid-session volume profile can be published as complete and approved next day

**File/functions:** [`backend/indicators/volume_profile.py` — `SingleStockVolumeProfile.process_tick` and `to_snapshot_dict`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/indicators/volume_profile.py#L95-L270); [`VolumeProfileEngine.save_session_profiles` and `load_previous_session_profile`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/indicators/volume_profile.py#L340-L515).

**Problem:** The first cumulative-volume observation establishes a baseline, but does not record that observation began after 09:15. Subsequent volume deltas accumulate only from that point. `to_snapshot_dict()` labels a profile `COMPLETE` when `gap_detected` is false and observed volume is positive. The save method persists any profile with positive observed volume. The previous-session loader validates symbol/token/date and level ordering, but does not reject a stored `DEGRADED` status or `gap_detected=true`; it sets `is_vp_allowed=True` once levels parse correctly.

**Failure scenario:** A user starts the stream at 11:00. The profile misses 09:15–11:00, but later trades add volume and the file can be saved as complete. The following session loads that partial profile and marks it allowed, even though it represents only part of the prior session.

**Fix:** Track the observed session start and require evidence of complete coverage before a profile becomes `COMPLETE` or `is_vp_allowed`. On first observation after session open, mark the profile incomplete unless the missing opening data is reconstructed from a validated source. Persist explicit `coverage_start`, `coverage_end`, `gap_detected`, source and status fields; reject previous profiles whose status is not `COMPLETE`, has any gap, belongs to a mismatched session, or lacks sufficient coverage. Keep partial profiles visible only as `DEGRADED` and never feed them into signal rules.

## RT-07 — HIGH: stream-start API says “started” before connection is confirmed

**Files/functions:** [`backend/routes/stream.py` — start route and response](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/routes/stream.py#L85-L128); [`MarketStreamManager.start_stream`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/market_stream_manager.py#L1030-L1215).

**Problem:** The manager can return `CONNECTING` after initiating the threaded KiteTicker connection. The API route translates this to `{"status":"started"}` without waiting for `on_connect` or confirming `StreamState.CONNECTED`.

**Failure scenario:** The handshake fails asynchronously, authentication is rejected by Kite, or the socket disconnects immediately. The client receives HTTP success and briefly reports a successful start even though the market feed is not live. The next status poll may eventually expose the failure, but the initiating response itself is inaccurate.

**Fix:** Return the manager’s actual state with a request/correlation ID. Only return `CONNECTED` after the callback confirms it; otherwise return `CONNECTING` or a failure state and surface timeout/last-error details. Test bad credentials, socket failure immediately after connect, reconnect and normal success.

## RT-08 — MEDIUM: process stream lock is acquired before many failure checks and is not released on each failure path

**Files/functions:** [`_acquire_process_stream_lock` / `_release_process_stream_lock`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/market_stream_manager.py#L230-L265); [`start_stream`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/market_stream_manager.py#L1030-L1205); [`_stop_internal` / `stop_stream`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/market_stream_manager.py#L1900-L2005).

**Problem:** `start_stream()` takes the cross-process file lock before verifying that an active Kite client, complete token map, saved session and `KiteTicker` are available. Several early exceptions set `ERROR` and raise without releasing the lock in a `finally` block. The lock may remain reserved until a later explicit stop or application shutdown.

**Failure scenario:** The first start attempt happens before Kite login or with a corrupted universe; the process owns the lock although no working stream exists. A second backend process cannot become the stream owner until the first process explicitly cleans up.

**Fix:** Use a scoped acquisition pattern: validate inputs first where possible, then acquire the lock immediately before stream ownership; on every startup exception, tear down any partially initialized ticker/worker state and release the lock. Add tests asserting the lock is free after every failure injection point. This is primarily a recovery/multi-process reliability issue; a later same-process retry may reuse the open descriptor, so the failure does not necessarily permanently stop that same manager.

## RT-09 — MEDIUM: Level-5 book queue drops can leave a symbol’s latest snapshot unprocessed

**File/function:** [`MarketStreamManager.start_stream` — `_on_book_update`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/market_stream_manager.py#L1405-L1468); book worker initialization and queue settings at [the manager constants/workers](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/market_stream_manager.py#L25-L55) and [worker setup](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/market_stream_manager.py#L280-L340).

**Problem:** `_on_book_update` stores the latest snapshot in `_pending_book_snapshots`, then queues the symbol. If `put_nowait()` raises `queue.Full`, the exception is swallowed. There is no guaranteed re-enqueue when the queue drains unless another update for that same symbol arrives.

**Failure scenario:** At a burst of depth updates the queue fills. A symbol’s last snapshot remains pending but is never evaluated if it receives no later update. The SSF strategy can wait on an old/absent book while the API reports no clear per-symbol dropped snapshot.

**Fix:** Use a bounded coalescing queue with a drain/reschedule invariant: every pending symbol must be queued or explicitly marked dropped/unavailable. On queue saturation, maintain a dirty-symbol set and repopulate it as capacity returns. Track counters by symbol and show a degraded-data state rather than silently swallowing backpressure.

## RT-10 — MEDIUM: stock volume profile uses a hard-coded 0.05 tick size for every symbol by default

**File/function:** [`VolumeProfileEngine.get_tick_size`, `register_instrument` and `initialize_universe`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/indicators/volume_profile.py#L270-L330).

**Problem:** `get_tick_size()` defaults all instruments to `0.05`; universe initialization does not look up each symbol’s actual tick size. `SingleStockVolumeProfile.round_to_tick()` uses that value to bucket price-volume observations. For securities whose permitted tick size differs, trades are assigned to incorrect price bins.

**Failure scenario:** A lower-priced stock with a smaller valid price increment has distinct trades rounded into the same 0.05 bucket. POC/value area and distance-to-level outputs can be wrong even if incoming trades, prices and volumes were genuine.

**Fix:** Derive tick size from authoritative instrument metadata or an explicit versioned symbol map, validate the value, and refuse to label volume-profile levels valid when tick size is unknown. Add boundary tests across instruments with different tick sizes.

## RT-11 — HIGH, conditional on remote deployment: data and trading-research endpoints are unauthenticated

**Files/functions:** [`backend/main.py` — router mounting and CORS](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/main.py#L80-L106); [`backend/routes/market.py`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/routes/market.py#L75-L145); [`backend/routes/signals.py` — scanner/journal/performance](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/routes/signals.py#L150-L205) and [journal/performance routes](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/routes/signals.py#L210-L334); [`backend/routes/stream.py` — live data](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/routes/stream.py#L125-L230).

**Problem:** Shared-secret dependencies protect selected control/research actions, not every route that exposes signals, market state, scanner output, journal records or performance. CORS origin restrictions are browser policy; they do not authenticate arbitrary HTTP clients.

**Failure scenario:** If the backend is exposed outside localhost, any reachable client can query the strategy feed and other unprotected read APIs. The scanner endpoint can also trigger a 700-stock REST quote/history operation; repeated requests can consume CPU, thread-pool capacity and Kite API rate budget.

**Fix:** If strictly local, bind to loopback and document that exposure boundary. For remote use, authenticate all private market/signal/journal endpoints, authorize user ownership, rate-limit expensive scanner/history calls, apply hard input bounds and separate public health status from protected trading data. Require `top_n` and `days` bounds; do not allow `refresh=true` to initiate an unrestricted history refresh from an unauthenticated request.

## RT-12 — MEDIUM: holiday calendar assumes every unknown year’s weekday is a trading day

**File/function:** [`backend/data/time_utils.py` — `HOLIDAYS_BY_YEAR` and `MarketCalendar.is_trading_day`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/data/time_utils.py#L95-L180).

**Problem:** The embedded holiday table covers 2023–2026. For a year not present, `is_trading_day()` returns `True` for every weekday. This also affects dates outside the listed range in historical warm-up and calendar calculations.

**Failure scenario:** After 2026, or during a backtest on an unlisted historical period, an NSE holiday is treated as a trading day. Warm-up logic may calculate the wrong previous session, expect candles on a holiday, or reject valid history as incomplete/stale.

**Fix:** Load a versioned official NSE holiday calendar for the evaluated date range. Unknown years must be `CALENDAR_UNAVAILABLE` instead of silently defaulting to weekdays-only. Add tests for known holidays, weekends, year changes and missing calendar data. Keep timezone/session date handling in IST.

## RT-13 — MEDIUM: cache data is schema-checked but scanner context identity is not proven

**File/function:** [`backend/scanner/stock_ranker.py` — cached historical context load and ATR/context calculation](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/scanner/stock_ranker.py#L430-L625); [`HistoricalDataLoader.load_cached_data_with_validation`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/data/historical_loader.py#L315-L380).

**Problem:** The historical loader validates candle schema, timestamps, OHLC ranges and duplicate timestamps. The scanner’s cached-context path validates whether it has the expected date/columns, but does not consistently prove that the sidecar metadata’s symbol, instrument token, interval and source match the requested symbol before using that context.

**Failure scenario:** A valid-shaped CSV is copied/renamed to another symbol’s cache path, or the sidecar does not match the file. The scanner can calculate ATR/average-volume context for the wrong security and combine it with the current symbol’s quote, producing a false ranking/candidate while every numeric column appears valid.

**Fix:** Treat cache identity as part of market-data validity: verify symbol, token, exchange, interval, exact date range and source in the sidecar; validate that rows belong to allowed sessions and requested interval; checksum/version the cache. If identity evidence is missing or inconsistent, discard it and refetch from Kite or return unavailable. Add a test that swaps two otherwise valid stock caches.

## RT-14 — MEDIUM: point-in-time universe helper silently falls back to today’s membership

**File/function:** [`backend/config/universe.py` — `get_universe(as_of=...)`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/config/universe.py#L15-L48).

**Problem:** The function prefers dated membership data when present, but falls back to the current large-cap 100 slice when a dated CSV is absent or has no usable date. For a historical `as_of`, that fallback is not point-in-time membership.

**Failure scenario:** Research or backtest code calls the point-in-time helper for an old date, but the membership CSV is missing. The calculation uses today’s surviving large-cap list, creating survivorship/membership bias and potentially overstating historical results.

**Fix:** If `as_of` is historical and dated membership cannot be proven, return an explicit unavailable/error state. Permit current-universe fallback only for an explicitly current-universe request and label it as such. Add a test proving that historical selection never uses current members when history is missing.

## RT-15 — MEDIUM: several documented environment variables are silently ignored

**Files:** [`.env.example`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/.env.example#L5-L18); [`backend/config/settings.py` — `AppSettings` and `extra="ignore"`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/config/settings.py#L290-L335).

**Problem:** The example exposes `MAX_CAPITAL_PER_TRADE`, `MAX_DAILY_LOSS_LIMIT` and `SEMI_AUTOMATED_CONFIRMATION`, but there are no matching fields in `AppSettings`; unknown settings are ignored. A developer can change these variables and receive no warning that they have no effect.

**Failure scenario:** Operators believe their configured caps/confirmation toggle are active when the application has not loaded or enforced them. The current active signal path is signal-only, so these variables do not configure live signal behavior as named.

**Fix:** Remove unsupported variables from the example or implement explicit typed consumers. Use startup validation to reject unknown configuration keys in production (or maintain an explicit allow-list), log a safe configuration summary and test that every documented setting maps to a real consumer. Do not imply that a control is active when it is not.

## RT-16 — HIGH for derivative research paths: hard-coded NIFTY lot size is obsolete

**File:** [`backend/config/settings.py` — `DEFAULT_NIFTY_LOT_SIZE`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/config/settings.py#L10-L16), plus config consumers such as `ResidualMomentumConfig` and `VRPConfig`.

**Problem:** The source defines the default NIFTY lot size as `75` and describes it as current. NSE's circular dated 3 October 2025 revised NIFTY market lot from 75 to 65 for the applicable contract cycle; Zerodha's current contract listings show 65 for current contracts. The per-equity live evaluator resolves share lot metadata separately, so this finding is not evidence that every equity signal uses 75. It does affect code paths that consume the shared NIFTY default and any research/backtest output that relies on it.

**Failure scenario:** An options/futures research path sizes a contract, estimates notional or costs, or compares outcomes using the obsolete lot size. Size- and P&L-derived metrics may be wrong by the contract-size ratio even though the strategy calculation itself executes.

**Fix:** Remove contract specifications from timeless constants. Resolve actual contract lot size from the broker instrument master for the required expiry; otherwise use an effective-dated specification table. Refuse calculations when contract metadata is unavailable. Add expiry-boundary tests that distinguish old and new contracts.

**External evidence:** [NSE circular, 3 October 2025](https://nsearchives.nseindia.com/content/circulars/FAOP70616.pdf); [Zerodha Futures margin calculator](https://zerodha.com/margin-calculator/Futures/).

## RT-17 — MEDIUM: background worker threads have no explicit stop/join lifecycle

**File/function:** [`backend/streaming/market_stream_manager.py` — `_ensure_background_workers_started`, `_flush_worker_loop`, evaluation/history/book worker loops](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/market_stream_manager.py#L288-L365).

**Problem:** The manager creates daemon worker threads whose loops are indefinite. Stream shutdown drains queues and closes the feed, but there is no shared stop event or explicit join lifecycle for these workers. A singleton restart may reuse them, so this alone does not prove a thread leak on every restart; it does mean app shutdown does not deterministically stop all owned work and future manager reinitialization/reloader paths require scrutiny.

**Failure scenario:** The app shuts down while a worker is in network/history/strategy work. The daemon is terminated with incomplete cleanup, or stale work completes against a later stream generation. Repeated lifecycle transitions can leave thread ownership and cleanup difficult to reason about.

**Fix:** Introduce a manager-owned stop event, make each loop interruptible, close/drain queues intentionally, and join workers within bounded time at shutdown. Preserve generation checks. Test repeated stream start/stop, failed startup, FastAPI lifespan shutdown, and development reload.

## RT-18 — MEDIUM: README contains commands and architecture that do not match the repository tree

**File:** [`README.md` — architecture and quickstart](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/README.md#L100-L220).

**Problem:** The architecture diagram names modules such as `backend/positions.py`, `backend/market.py`, `backend/stream_routes.py`, `backend/signals.py`, and `backend/kite.py`, while current route implementations are under `backend/routes/`. The README commands reference `run_algo.py`, which is not present in the current tracked tree. The claimed “166+ tests passing” is documentation, not a test result reproduced during this audit.

**Failure scenario:** A developer follows an obsolete run/scan/backtest command, diagnoses the wrong module or assumes a suite is green without checking the actual branch. This increases the chance that fixes target dead paths rather than the canonical live path.

**Fix:** Regenerate architecture and quickstart from actual entrypoints, include a verified test command and latest verified test result with commit SHA, and remove references to missing scripts/modules. Have CI check documented commands.

## RT-19 — MEDIUM: database and generated cache artifacts are tracked in the public repository

**Files:** `backend/database/trading_system.db` (tracked binary); 1,040 tracked files are under backend cache metadata paths in the inspected tree. The project `.gitignore` excludes database/cache artifacts, but that does not remove files already tracked.

**Problem:** A local SQLite database and numerous generated cache metadata files are present in the tracked tree despite ignore rules. I did not inspect the SQLite contents and do not claim it contains private or sensitive records. Its presence means the repository is carrying runtime state and should be reviewed for any personal, trade, signal, or session-related records before further public distribution. Metadata sidecars can also diverge from untracked CSV payloads and confuse reproducibility.

**Failure scenario:** Someone clones the project and receives stale runtime/journal state, or the database contains records the owner did not intend to publish. Generated metadata may describe a cache file that is absent or changed, producing confusing cache validation behavior.

**Fix:** Inspect the tracked database locally for sensitive or user-specific contents; remove it from version control and history if inappropriate. Untrack generated cache directories and keep only reproducible fixtures that are intentionally versioned. Add a CI check preventing database/session/cache files from reappearing. Do not delete local runtime data blindly before backing it up.

## RT-20 — MEDIUM: multiple Uvicorn workers would split process-local stream state

**Files:** [`backend/streaming/live_market_state.py`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/live_market_state.py) and [`backend/streaming/market_stream_manager.py`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/market_stream_manager.py).

**Problem:** Market data, prediction state, queues, and the manager are in-process singletons. The filesystem lock deliberately prohibits more than one active stream owner; it does not make other worker processes share the owner's Python memory. The checked-in development command runs one Uvicorn worker by default, so the defect is conditional on production deployment using multiple workers/processes.

**Failure scenario:** Worker A owns the Kite stream and populates its in-memory state. A request lands on worker B, whose memory is empty or stale. The API returns no signals or disconnected-looking market state despite worker A receiving ticks; starting from worker B may fail on the process lock.

**Fix:** Explicitly enforce one backend worker for this architecture, or move the stream to a dedicated service and publish canonical state through shared storage/message transport. Add a deployment guard that rejects unsupported worker counts and document that `--reload` is development-only.

## RT-21 — LOW/MEDIUM: future-dated timestamps can appear fresh in aggregate health

**Files:** [`backend/streaming/market_stream_manager.py` — `get_status`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/market_stream_manager.py#L1990-L2075); [`backend/routes/system.py` — health classification](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/routes/system.py#L20-L80).

**Problem:** Aggregate tick-age calculation clamps a negative age to zero. Elsewhere, symbol-level checks generally reject negative ages, so this is chiefly an observability/consistency mismatch, not proof that strategy validation accepts every future timestamp.

**Failure scenario:** A bad exchange timestamp or server-clock skew makes the aggregate feed show age zero and health look fresh even while per-symbol freshness or strategy freshness rejects that data.

**Fix:** Preserve negative age as an invalid/future-timestamp state; classify the feed as clock-skewed/degraded instead of clamping it. Add tests for future, missing, naive, and timezone-aware timestamps.

---

## RT-22 — HIGH: volume-profile accumulator does not roll over when the session date changes

**File/function:** [`backend/indicators/volume_profile.py` — `SingleStockVolumeProfile.process_tick`, `VolumeProfileEngine.process_tick`, and `initialize_universe`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/indicators/volume_profile.py#L80-L350).

**Problem:** The engine registers each profile for a `session_date` at initialization. On an incoming tick for an existing symbol, `VolumeProfileEngine.process_tick()` calls the existing profile; it does not compare the tick date to the profile’s date or create a new profile. The accumulator itself also does not clear the price-volume histogram on a date change. A cumulative-volume reset is only treated as a baseline adjustment; it does not start a new session histogram.

**Failure scenario:** If the app/stream stays alive into a new session, current-day trades can be added to yesterday’s profile. The snapshot can retain yesterday’s `session_date`, write into yesterday’s filename, contaminate developing POC/value area, and cause later reads to associate mixed-session values with the wrong date. The connection-gap state also needs explicit recovery semantics rather than silently treating a reconnect as a clean session.

**Fix:** On every tick, compare the normalized exchange date with the profile’s session date. On a legitimate new session, finalize and persist the previous profile, register a clean new profile, then start from a verified cumulative-volume baseline. Treat a same-day reconnect as degraded until complete recovery can be proven. Test consecutive trading days, cumulative-volume reset, missed session open, reconnect and late/out-of-order ticks.

## RT-23 — MEDIUM: SSF-L5-SRM backtest does not test its live order-book strategy

**Files:** [`backend/routes/backtest.py` — `_make_factory`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/routes/backtest.py#L76-L110); [`backend/strategy/prediction_service.py` — live SSF evaluation](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/strategy/prediction_service.py#L2537-L2635).

**Problem:** The backtest factory explicitly states that 15-minute OHLCV has no order-book depth and “only exercises its regime gate.” The live evaluator, by contrast, requires a real fresh Level-5 book and invokes `on_book_update()`. Those are not equivalent strategy inputs or behaviors.

**Failure scenario:** A comparative backtest table presents a return/accuracy-like result for SSF-L5-SRM that a reader interprets as performance of the production microstructure signals, even though the historical replay cannot trigger the same book-dependent logic.

**Fix:** Mark this strategy backtest as unsupported/incomplete unless timestamp-aligned Level-5 history is available. Do not include its output as directly comparable production-strategy performance. The API response should explicitly identify missing inputs and the dimension that was tested. Add a test that this route cannot label a regime-only replay as a full SSF backtest.

---

# F. Complete source-area audit matrix

This matrix records the primary source areas inspected and the result of this static pass. “No finding listed” does not mean mathematically bug-free; it means no additional issue was confirmed from the inspected path without stronger evidence or runtime reproduction.

| Area | Files inspected / assessed | Result |
|---|---|---|
| Startup and route wiring | `backend/main.py`, `backend/routes/__init__.py`, route modules | Canonical routes are in `backend/routes/`; README paths are stale. Read paths are broadly unauthenticated. |
| Kite session and broker | `backend/broker/kite_adapter.py`, `backend/routes/kite.py`, `backend/security.py` | Session file permissions/atomic write are positive controls. Shared-secret handling in browser is not a security boundary; startup secret placeholder is not rejected. |
| Settings and universe | `backend/config/settings.py`, `backend/config/universe.py`, `backend/data/universe/*.json` | Non-fatal master validation, ignored environment parameters, stale NIFTY default; universe input is versioned JSON rather than dynamically proven current membership. |
| Instrument mapping | `backend/data/instrument_resolver.py` | NSE index alias maps NIFTY to cash index; that mapping is misused by default futures backtest. Cache resolver has separate identity/expiry behavior that should remain covered by regression tests. |
| Historical data | `backend/data/historical_loader.py`, `backend/scanner/history_context_warmer.py`, cache usage | Loader performs useful schema/OHLC/duplicate checks and raises on zero fetched candles. Consumer-level cache identity must be proven for the requested symbol/token/interval, not inferred from valid columns alone. |
| Candle aggregation | `backend/data/candle_aggregator.py` | Has explicit symbol/token validation, out-of-order tick handling in manager, candle closure and gap tracking. Runtime reconnect, startup mid-session and cumulative-volume resets still need end-to-end tests. |
| Live streaming | `backend/streaming/market_stream_manager.py`, `backend/streaming/live_market_state.py` | Bounded/coalesced queues and counters exist; start reporting/lock cleanup/worker lifecycle/L5 queue overflow and multiprocess deployment remain concerns. |
| Live signal engine | `backend/streaming/live_signal_engine.py` | Strategy exceptions are converted to per-strategy errors; cached freshness is refreshed but stale directional results are not withdrawn at serving time. |
| Strategy adapter/consensus | `backend/strategy/prediction_service.py`, `base_strategy.py`, ORB, CPR, Dual EMA, APEX, Sector Impulse, SSF, AOU-OSS, CRSD strategy modules | Shared candle preparation and per-strategy exception isolation exist. Live signal freshness defect is at the serving boundary; SSF backtest does not exercise live book logic. No claim of profitable or robust performance is possible from this static source pass. |
| Scanner | `backend/scanner/stock_ranker.py`, `liquidity_filter.py` | Fails unavailable for several missing quote/context cases, but missing order-book depth can bypass spread validation; historical context identity is not consistently proven. Ranking score is a heuristic ranking score, not a calibrated probability of winning. |
| Indicators | `backend/indicators/vwap.py`, anchored VWAP, FVG, liquidity sweep, order-book imbalance, swing structure, volume profile | Several indicator engines are standalone helpers; no evidence that every helper is part of the canonical strategy path. Volume profile has completeness, tick-size and date-rollover defects. |
| API endpoints | `backend/routes/kite.py`, `market.py`, `stream.py`, `signals.py`, `system.py`, `backtest.py` | Sensitive stream actions/backtest routes use the shared-secret dependency. Quote/signal/journal/system/status reads are not authenticated in the code path inspected. Expensive scanner calls need abuse/rate controls for non-local deployments. |
| Database and journal | `backend/database/db.py` | SQLite WAL/busy timeout and an in-process lock help local concurrency. They do not coordinate independent worker processes. Tracked DB requires local content review; contents not inspected. |
| Backtesting | `backend/backtest/*`, `backend/routes/backtest.py`, tests under `tests/backtest/` | Default NIFTY token/config mismatch; SSF backtest is regime-only. A full live/backtest equivalence or bias certification was not performed. |
| Frontend | `frontend/src/api/*`, `frontend/src/pages/*`, `frontend/src/components/*`, `frontend/src/hooks/usePolling.js` | Polling-based UI is clear, but price freshness is treated separately from signal freshness and the latter is not enforced. Browser-bundled secret is recoverable by its user. |
| Tests | `backend/tests/*`, `tests/*`, `pytest.ini` | Test inventory and relevant test names were reviewed, but tests were not executed. The README pass-count claim remains unverified. |
| Docs/build/config | `README.md`, `.env.example`, `requirements.txt`, package manifests, Vite config, `.gitignore` | README references missing scripts/modules, three documented env controls have no settings consumers, and generated runtime/cache files remain tracked. |

## G. Data integrity summary

| Data item | Main path | Audit assessment |
|---|---|---|
| LTP | Kite tick → aggregator/state → signal route/UI | Current LTP is checked for positivity/freshness in primary paths; it can be displayed beside stale strategy direction (RT-01). |
| OHLC | Historical loader or ticks → candle aggregation → prepared candles | Several validation gates exist. Market/session continuity is conditional on the caller’s checks and cache identity. |
| Volume | Kite cumulative volume → incremental candle volume/profile | Cumulative resets are handled as baselines, but the volume-profile accumulator does not reliably isolate session dates (RT-22). |
| VWAP | Quote ATP or observed tick/volume aggregation; historical session VWAP helper | Missing VWAP is rejected by `PredictionService`; `calculate_session_vwap` fills zero-volume rows with typical price, which is an estimate/fallback behavior that must not be treated as independent traded VWAP evidence. |
| Bid/ask/depth | Kite quote or ticker depth → `BookSnapshot`/scanner | Book parsing validates levels in the streaming path; scanner’s liquidity filter accepts `None` depth without proving spread (RT-04). |
| Tokens/symbols | Instrument master → resolver/cache → stream map | Universe token resolution rejects incomplete maps, but malformed universe source can shrink the expected set. NIFTY cash-index token is confused with futures in the default backtest (RT-02/03). |
| OI/futures | Ticker futures/index subscriptions → SSF context store | This area requires live-contract and reconnect verification; no end-to-end OI accuracy assertion was made without live replay. |
| Time/session | Exchange timestamps → IST normalization → age/session checks | Multiple IST helpers and freshness gates are positive controls. Calendar coverage and aggregate negative-age clamping remain defects (RT-12/21). |
| Volume profile levels | Tick price + volume increments → POC/VAH/VAL → saved JSON | Incomplete observation may be presented as complete; price increments are defaulted to ₹0.05; next-session rollover is not handled by existing profile (RT-06/10/22). |


# H. Streaming, concurrency and queue assessment

The manager has several sound defensive measures: it tracks stream generations, drops explicitly out-of-order ticks, bounds several queues, tracks dropped/coalesced counts, uses history states, and prevents a second stream owner via a process lock. Those protections do not remove the confirmed issues below.

| Concern | Current behavior | Remaining risk / required proof |
|---|---|---|
| Stream connect | `KiteTicker.connect(threaded=True)` is asynchronous; route can answer “started” while state is only `CONNECTING` | UI/automation must wait for confirmed `CONNECTED` plus fresh equity ticks (RT-07). |
| Startup failure | Process lock is acquired before credential, token-map and ticker validation; some raises happen before a symmetric release path | A failed startup can keep the OS lock held until explicit stop/process cleanup (RT-08). |
| Evaluation workers | Symbol-keyed pending map coalesces tasks; generation and age checks prevent some stale work | Queue pressure can drop evaluations. A fresh tick must never imply the last strategy evaluation also succeeded; RT-01 plus worker counters need monitoring. |
| L5/book queue | Latest snapshot per symbol is stored, but a full queue catches `queue.Full` without guaranteed rescheduling of that symbol | Final snapshot can remain pending and unprocessed absent a later update; counters must drive a degraded status (RT-09). |
| Historical refresh | Separate bounded queue, concurrency semaphore and rate limiter exist | Validate restart/session boundaries, rate-limit stalls and failure recovery with fault injection. Static review cannot establish behavior under live Kite outages. |
| Thread lifecycle | Workers are daemons with long-running loops | No deterministic stop event/join path (RT-17). |
| Process boundaries | A lock prevents multiple stream owners, but state is in process memory | Multiple API workers/processes split ownership and data reads (RT-20). |
| Stress | Queue sizes and worker count are configured and surfaced in status | No verified 10x-tick, 700-symbol market-open or slow-client load test was run. Do not assume throughput from queue capacity alone. |

## I. API and authentication inventory

This is the observed route-level exposure from the inspected routers; it is not a penetration test of an internet-hosted instance.

| API group | Observed access behavior | Audit action |
|---|---|---|
| Kite login/callback/status | Login flow needs to support the browser’s OAuth callback; status is a read endpoint | Keep secrets server-side; avoid exposing access tokens or request-token values in responses/logs. Full deployment auth/cookie behavior needs runtime check. |
| Kite logout | Uses shared-secret protection in the inspected path | Do not rely on a browser-bundled shared secret for a remote deployment. |
| `/api/stream/start`, `/api/stream/stop` | Shared-secret dependency | Verify secret is configured with a strong value, but remove client-distributed shared secrets for remote use. |
| `/api/stream/status`, `/api/stream/signals`, `/api/stream/market` | No authentication dependency in inspected route definitions | Bind to loopback for local-only use or require real server-side auth for remote access. Signal endpoint freshness must be enforced (RT-01). |
| `/api/market/prices`, volume profile | No authentication dependency in inspected route definitions | Protect private use or explicitly treat as public market-data service; apply request limits. |
| `/api/strategy/state`, scanner, performance and signal journal | No authentication dependency in inspected route definitions | These endpoints expose system state and can trigger quote/history work. Authenticate and rate-limit for public deployment (RT-11). |
| `/api/research/backtest`, `/api/strategy/backtest` | Router is attached with shared-secret dependency; backtest can be disabled by env flag | Validate request bounds and output semantics; SSF result is only regime gate (RT-23), and default NIFTY input is wrong (RT-02). |
| `/api/system/health` | Public read endpoint in inspected wiring | Avoid including secrets, local paths or internal exception payloads; return an explicit degraded/unknown state when dependencies cannot be checked. |

### Security boundary conclusion

The default CORS allowlist is local-development oriented and explicitly avoids `*`, which is useful but not authentication. If the service is exposed on a LAN or internet, CORS does not prevent scripts, curl or direct HTTP clients from reaching endpoints. The shared secret can be configured as a Vite `VITE_*` frontend value, which is delivered to the browser; a user who can access the site can recover it. The example secret placeholder must never be accepted as a production value. The inspected backend’s active path is signal-only; no `place_order`, `modify_order` or `cancel_order` call was identified in the primary files examined, but this statement is not a substitute for automated repository-wide order-call scanning and runtime egress restrictions.

## J. Strategy independence and consensus

**Positive controls observed:** `PredictionService._evaluate_all()` defines a separate evaluator per strategy and wraps each call in its own exception handler. One strategy exception becomes that strategy’s error prediction instead of aborting evaluation of all other strategies. Stateful AOU/CRSD runtimes use per-symbol state and locks; SSF has an explicit runtime owner.

**Remaining limits:** The strategies intentionally share the validated `_PreparedCandles` object and receive common LTP/broker/context inputs. Several stateful strategies depend on the stream manager’s runtime lifecycle and history readiness. A static read cannot prove there are no mutable aliases inside every strategy class, and no stress test with concurrent ticks/evaluation/reconnect was executed. Keep the existing strategy-independence tests and extend them to assert that changing one strategy’s output/runtime state never changes any other strategy’s output for the same fixed input.

**Consensus contract required:** Only validated, directional, fresh signals should vote; `WAITING`, `NO_TRADE`, `UNAVAILABLE`, `ERROR`, `NOT_APPLICABLE`, and stale results should not count as supportive votes. The stale serving defect means even a sensible consensus calculation at evaluation time can become obsolete afterward. Recompute consensus after all serve-time invalidations and report both total configured strategies and number of currently evaluable strategies; do not present vote ratio as a calibrated probability.

## K. Configuration / parameter audit

| Parameter or group | Source/default | Consumer / status | Required correction |
|---|---|---|---|
| `APP_SHARED_SECRET` | `AppSettings.app_shared_secret` defaults to empty; `.env.example` shows a placeholder | Shared-secret dependency returns 503 when empty, but no clear startup rejection for the known placeholder; frontend also attempts to load a Vite secret | Reject placeholder at startup and remove front-end secret distribution. |
| `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_ACCESS_TOKEN` | AppSettings env fields | Broker/session creation | Validate missing/malformed config safely; never log token or callback request parameters. A missing API secret at OAuth exchange should stop authentication, not fallback to unrelated credentials. |
| `MAX_CAPITAL_PER_TRADE` | `.env.example` | No matching AppSettings model field identified; `extra="ignore"` silently ignores extras | Delete or add typed consumer. Do not advertise a no-op setting. |
| `MAX_DAILY_LOSS_LIMIT` | `.env.example` | No matching AppSettings field/active live consumer identified | Delete or add actual settings; do not imply enforcement. |
| `SEMI_AUTOMATED_CONFIRMATION` | `.env.example` | No matching AppSettings field/active live consumer identified | Delete or connect to a real documented workflow; it is not live order confirmation in this signal-only path. |
| `DEFAULT_NIFTY_LOT_SIZE` | `75` in `backend/config/settings.py` | NIFTY-related config defaults / research consumers; obsolete for current relevant contract cycle | Use contract-specific effective-dated instrument metadata (RT-16). |
| Liquidity thresholds | `LiquidityFilterConfig` default values in settings | Scanner liquidity filter | Validate ranges and require actual bid/ask evidence for spread checks (RT-04). |
| Candle/result freshness limits | `PredictionService` class constants; signal engine has its own age thresholds | Prep/finalize/serve layers | Centralize or explicitly document why tolerances differ. The API/frontend must enforce freshness after caching (RT-01). |
| Worker/queue config | `MARKET_STREAM_EVALUATION_WORKERS`, queue sizes and ages in stream manager | Streaming worker loops and coalescing | Validate startup config; expose thresholds and explicit “overloaded/degraded” states. Add a backpressure stress test. |
| Holiday source | `HOLIDAYS_BY_YEAR` static mapping | Calendar/trading-day and previous-session functions | Use effective-dated, versioned market calendar; unknown coverage must fail closed (RT-12). |
| Quote freshness | Scanner 120-second quote-age check; strategy LTP/book checks have tighter configurable/class-level limits | Scanner vs live evaluator | Ensure route state and strategy-state freshness are separate and visible (RT-01). |
| Universe size/category expectations | Comments/docs say 700/100/100/500; actual file controls `expected_symbols` | Scanner and stream token mapping | Assert exact counts independently of loaded file. |
| `VITE_APP_SHARED_SECRET` | Frontend build-time variable in `frontend/src/api/client.js` | Browser request header | Must not be treated as secret; move auth to real user session/server-side proxy. |

## L. Failure cascades: how this fails in real-world operation

1. **Ticks continue, evaluator stalls:** new prices reach `live_market_state`; evaluation queue is delayed or drops a candle; the signal engine retains its prior directional payload; the route sees a fresh tick and returns the old direction; UI renders a current quote and old signal together. **Outcome:** stale actionable-looking signal. Prevention: RT-01 and monitoring of last successful evaluation per symbol/strategy.
2. **One malformed universe row:** source file logs validation problems and uses the remaining records; token resolution succeeds for the reduced set; startup compares against this same reduced set; the stream connects. **Outcome:** some symbols are missing while status can appear connected. Prevention: RT-03.
3. **Default NIFTY backtest:** request selects default NIFTY settings as futures; resolver returns canonical NSE index token; historical API downloads index bars; backtester calculates report using futures config/costs. **Outcome:** performance report is mislabeled as tradable futures evidence. Prevention: RT-02 and cache invalidation.
4. **Scanner depth missing:** quote contains valid LTP/volume/context but no depth; `depth_data is None` skips spread checks; remaining tests pass. **Outcome:** a candidate can be marked tradable without a proven spread. Prevention: RT-04.
5. **Start from mid-session:** volume profile starts observing at first tick, so its opening volume segment was not captured; a later observed increment exists; snapshot marks profile complete if no gap flag was triggered; next session’s loader trusts valid POC/VAH/VAL and forces `is_vp_allowed=True`. **Outcome:** profile levels appear authoritative without full-session volume coverage. Prevention: RT-06.
6. **App stays alive overnight:** profile instances retain yesterday’s date/histogram; next day’s tick stream is processed by the existing object and cumulative-volume reset only resets the baseline. **Outcome:** mixed-session price/volume bins, wrong date labels, and possible overwrite of prior file. Prevention: RT-22.
7. **Stream start fails before ticker creation:** process lock has been acquired; token/session validation raises; no guaranteed `finally` releases the lock. **Outcome:** a later backend process cannot acquire stream ownership until prior process cleanup. Prevention: RT-08.
8. **User deploys two API workers:** worker A owns the stream and its local state; worker B handles `/api/stream/signals` with independent memory. **Outcome:** intermittent missing data/status and confusing start errors. Prevention: RT-20.
9. **Remote API exposure:** endpoints expose market/system/signals/journal without authentication; the shared secret is compiled into frontend assets. **Outcome:** endpoint access is not prevented by CORS and protected routes’ secret can be extracted by site users. Prevention: RT-05/11 and a proper auth boundary.
10. **Holiday not in calendar:** the weekday rule returns true for an unlisted year holiday; history warmer expects an incorrect previous session. **Outcome:** cache warm-up, latest-session selection or staleness decisions can be wrong. Prevention: RT-12.
11. **Wrong cache file under a stock name:** columns and OHLC values are valid, but symbol/token sidecar identity isn’t fully enforced at scanner context consumption. **Outcome:** ATR/average volume from one security modifies another security’s ranking. Prevention: RT-13.
12. **SSF backtest judged as live performance:** offline 15-minute data feeds a regime gate, while live signals require book depth. **Outcome:** report is not evaluating the production microstructure strategy. Prevention: RT-23.
13. **Tick-size differences:** volume profile uses the same ₹0.05 bin default for every stock. **Outcome:** profile buckets can be misaligned for securities with different price increments, shifting POC/value area. Prevention: RT-10.
14. **Operator changes `.env.example` caps:** unsupported variables are ignored. **Outcome:** operator believes risk/capital/confirmation settings changed when they did not. Prevention: RT-15.


## RT-24 — MEDIUM: seeded synthetic scanning remains callable inside the production scanner

**File/function:** [`backend/scanner/stock_ranker.py` — `scan_universe(..., allow_synthetic=False)` and `_scan_synthetic`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/scanner/stock_ranker.py#L341-L465).

**Problem:** The scanner implementation includes `_scan_synthetic(seed=42)` and an `allow_synthetic` argument that can enable it when real Kite access fails. The inspected `/api/strategy/scanner` route explicitly passes `allow_synthetic=False`, so this is not evidence that the canonical API currently substitutes synthetic scanner results. It is still callable via another internal caller or a future route that accidentally opts in, which conflicts with the project's stated rule that production data must never fall back to synthetic values.


**Failure scenario:** A developer calls `scan_universe(allow_synthetic=True)` to make an unavailable scanner “work” during a live run. Seeded synthetic metrics can be returned with a synthetic source marker, but a consumer that ignores that marker may show them as genuine candidates.

**Fix:** Remove the synthetic path from the production scanner module, or move it into a test-only module that production routes/config cannot enable. Retain fail-closed behavior when Kite quotes/history are unavailable. Add a repository-level test that production scanner entrypoints never emit synthetic rankings.

---

# M. Technical debt and code-quality assessment

The largest code-quality risks are correctness-boundary problems, not style issues:

1. **Duplicated freshness concepts:** scanner quotes, stream health, per-symbol tick freshness, per-strategy data freshness, completed-candle freshness, request-to-result delay and serving-time freshness coexist. The API currently treats one fresh tick as enough to serve old strategy output. Create one documented signal-validity contract and propagate its states without collapsing them.
2. **Configuration drift:** documented settings do not match actual Pydantic fields/consumers, and a dated derivative specification is hard-coded as timeless.
3. **Data identity drift:** syntactically valid candle files can still be wrong for the requested symbol/token/source; schema validation and provenance are different checks.
4. **Session-state drift:** live ticker, candle aggregator, background workers, volume-profile state and caches have separate reset semantics. Session rollover and reconnect should be tested as one end-to-end operation.
5. **Research/live semantic drift:** a strategy name does not guarantee the same inputs or execution semantics in a backtest. SSF's regime-only replay and the NIFTY index/future mix-up make this concrete.
6. **Documentation/build drift:** README paths and commands describe a different layout from the actual tree.
7. **Tracked generated state:** the database and cache metadata in source control compromise clean-clone reproducibility and require content review.

No broad refactor is justified by this report alone. Fix the smallest violated contract at its source, add a test for the exact failure mode, then rerun tests and the related integration scenario.

# N. Recommended fix order

## P0 — Before interpreting any live signal

1. **RT-01 — stale directional result remains visible.** Invalidate cached predictions at API serve-time and recompute consensus after invalidation; make the frontend enforce the same contract.
2. **RT-03 — universe can be incomplete while startup proceeds.** Make master validation fail closed against explicit expected counts and required schema.
3. **RT-04 — missing depth can pass spread gate.** Missing/invalid spread inputs must make the stock untradable/unavailable.
4. **RT-05 and RT-11 — secret is exposed to the browser and reads are not authenticated.** Separate local-only binding from remote deployment; implement server-side auth before exposing the service beyond localhost.
5. **RT-06 and RT-22 — volume-profile completeness and date rollover.** Treat any partial session/reconnect gap as degraded; never approve it as complete without proof.
6. **RT-07 and RT-08 — stream state/cleanup.** Report actual connection state and release ownership on every startup failure path.

## P1 — Before relying on scanner rankings or research/backtests

7. **RT-02 — default NIFTY backtest maps cash-index token to futures config.** Fix instrument type, token, expiry and cache invalidation before reading historical reports.
8. **RT-13 — scanner cache identity.** Bind historical context to symbol, token, exchange, interval and target date.
9. **RT-12 — finite holiday table.** Unknown calendar coverage must not be treated as a confirmed trading day.
10. **RT-16 — stale contract-size default.** Use actual contract master/effective-dated lot-size spec.
11. **RT-23 — SSF backtest semantics.** Stop representing a regime-only replay as performance evidence for a live order-book strategy.
12. **RT-14 — point-in-time universe.** Historical research must not silently use current constituents.

## P2 — Reliability and auditability

13. **RT-09, RT-17, RT-20, RT-21:** book queue recovery; explicit worker shutdown; supported single-worker deployment; future timestamps marked invalid.
14. **RT-10:** derive volume-profile tick size from instrument metadata.
15. **RT-15, RT-18, RT-19:** reconcile config/documentation and review/untrack generated database/cache state.
16. **RT-24:** remove synthetic fallback from the production scanner module.
17. Add fault-injection, session-rollover and market-open stress tests before reclassifying the system as ready.

# O. Required verification plan after fixes

The following commands are required acceptance checks, not claimed results. This audit did not run the full suite or connect to live Kite.

### 1. Baseline tests

From repository root:

```bash
source .venv/bin/activate
pytest -q
```

Record the commit SHA, test count, failures and skipped tests. Do not rely on the README's historical pass-count statement. Repeat after each defect cluster to localize regressions.

### 2. Deterministic regression tests

- Create a directional cached prediction, advance beyond its allowed result/candle age while continuing to feed ticks, call `/api/stream/signals`, and assert the stale direction is withheld and consensus no longer votes for it.
- Remove one stock from `700_stocks.json`; duplicate a symbol; corrupt rank/category; duplicate tokens. Each defect must prevent a successful stream start.
- Call the liquidity filter with `depth=None`, empty depth, malformed top-of-book, crossed bid/ask and stale timestamps. All unknown cases must fail closed.
- Start a stream with no Kite client, bad token map, missing session and ticker connection failure. Assert the process lock is released on every failure path.
- Fill the book queue and then stop sending updates for a symbol. Assert the final coalesced snapshot is processed or an explicit degraded/drop condition is emitted.
- Start volume profiling mid-session, trigger reconnect, roll to the next date and reset cumulative volume. Assert the prior profile is not marked complete, next session starts empty, and prior-session files are not overwritten with mixed dates.
- Swap two otherwise valid stock cache files/metadata; scanner context must reject the identity mismatch.
- Check a known holiday, weekend and unconfigured calendar year. Unknown coverage must not be classified as a confirmed trading day.
- Verify default NIFTY backtest resolves an actual NFO FUT contract or returns a clear unsupported result; it must never pair an NSE index token with a futures config.
- Verify the SSF backtest refuses to publish a production-comparable result when historical depth is absent.
- Assert no production scanner entrypoint can opt into synthetic rankings; Kite input failure must return unavailable/error instead.

### 3. Integration and stress tests

- Authenticate with an approved development Kite account, verify real exchange timestamps and token identity, stream a small universe, and compare observed ticks/aggregated candles/volume against broker data.
- Replay missing/duplicate/out-of-order ticks, future timestamps, disconnect/reconnect, stale LTP, delayed history warm-up, invalid JSON cache, rate-limit responses and database lock contention.
- Simulate market open for 700 symbols with 10x normal tick volume, slow historical API, slow frontend and repeated polling. Track CPU, memory, evaluation age, queue age, dropped work, rejected ticks, reconnect duration and per-symbol last-successful evaluation.
- Exercise a full date boundary without process restart, shutdown with tasks in flight and development reload. Assert worker count does not grow and no prior-session candle/profile leaks into the next session.
- Inspect browser production assets to confirm no server secret is present. Test unauthenticated access to every private endpoint and require 401/403 rather than sensitive data.

### 4. Live canary acceptance gates

Do not mark the system ready because the dashboard loads or a scanner returns 700 rows. Before a user can rely on a direction, require all of the following:

- Kite authenticated and ticker `CONNECTED`.
- Fresh equity tick and per-symbol LTP with exchange timestamp.
- Valid completed, gap-checked source candle for that strategy.
- Successful evaluation timestamp inside the configured result-age budget.
- Strategy result and source candle marked fresh at API serve time.
- Consensus recomputed only from currently valid strategy outputs.
- Every missing required input shown as `UNAVAILABLE` / `ERROR` / degraded, never a valid default.
- Instrument identity, exchange and cache provenance verified.
- No unresolved CRITICAL/HIGH finding affecting that path.

# P. Final verdict

| Question | Verdict | Reason |
|---|---|---|
| Can the system consume real Kite data? | **Conditionally, yes — not proven production-safe.** | Broker adapter, timestamp validation, tick checks and historical loading have real-data paths; live end-to-end correctness under reconnect/rate-limit conditions was not executed. |
| Can it safely display live signals today? | **NO-GO until RT-01 is fixed and verified.** | Old directional predictions/consensus can remain visible while the current quote remains fresh. |
| Can stale/incorrect data reach a strategy? | **YES, in some paths/conditions.** | A stale result can be served; malformed universe/cache identity can contaminate source selection or scanner context. Strategy candle preparation does reject a number of invalid/gapped/stale inputs; this is not a claim that every path accepts stale data. |
| Can a strategy generate a false signal? | **Yes, possible.** | Wrong input identity/freshness can corrupt levels and direction. This audit does not claim that every strategy's logic is wrong. |
| Can one strategy affect another? | **No direct cross-vote dependency was proven in the inspected evaluator.** | Strategies have separate evaluators and per-strategy exception handling, but shared prepared inputs and runtime lifecycle need concurrency regression coverage. |
| Can a streaming failure go unnoticed? | **YES.** | Start can be reported before `CONNECTED`; a full book queue can drop processing; a fresh tick can coexist with stale strategy output. Some health counters already exist. |
| Can API/data failures look valid? | **YES.** | Missing scanner depth can pass; universe defects are logged but non-fatal; synthetic scanner mode remains callable; volume-profile completeness can be overstated. |
| Can the system fail silently? | **YES, in material cases.** | Some queue-full errors are swallowed; some failures become missing/degraded output without a strong aggregate alarm. Not every failure is silent—several counters and logs already exist. |
| Was live order placement identified? | **No order-placement call was identified in the primary active files examined.** | The intended system is signal-only. A repository-wide executable scan/runtime egress test was not run, so this is not a formal security guarantee. |

## Top 10 defects to fix first

1. **Stale directional result remains visible** — `backend/streaming/live_signal_engine.py`, `backend/routes/stream.py`, `frontend/src/pages/LiveSignalsPage.jsx` (RT-01).
2. **Universe schema/count defects do not abort startup** — `backend/config/universe.py`, `backend/streaming/market_stream_manager.py` (RT-03).
3. **Missing bid/ask depth can pass liquidity filter** — `backend/scanner/liquidity_filter.py` (RT-04).
4. **Browser-bundled shared secret and unprotected read APIs** — `frontend/src/api/client.js`, `backend/security.py`, `backend/routes/*.py` (RT-05/11).
5. **Volume-profile session completeness can be falsely approved** — `backend/indicators/volume_profile.py` (RT-06).
6. **Default NIFTY backtest maps an index token to a futures config** — `backend/routes/backtest.py`, `backend/data/instrument_resolver.py` (RT-02).
7. **Profile accumulator does not reset at a new session date** — `backend/indicators/volume_profile.py` (RT-22).
8. **Stream reports started before connection is verified** — `backend/routes/stream.py`, `backend/streaming/market_stream_manager.py` (RT-07).
9. **Stream process lock may survive failed startup** — `backend/streaming/market_stream_manager.py` (RT-08).
10. **Historical scanner cache identity is not fully proven** — `backend/scanner/stock_ranker.py`, `backend/data/historical_loader.py` (RT-13).

**Bottom line:** This is a substantial static audit of the current source snapshot, not live-market certification. The signal-freshness defect and NIFTY backtest mapping defect are code-evidenced and must be fixed before relying on those outputs. Other high-severity issues affect universe integrity, liquidity eligibility, authentication and volume-profile validity. No code was changed; no tests are claimed to have passed as a result of this audit.


---

# Continuation findings from the resumed source trace (RT-25–RT-30)

These findings were added after tracing the current GitHub source commit `c6a2a58a1937c97ed711ff259f97421b1170d977`. They are source-level findings, not runtime reproduction. They do not change the audit status: **INCOMPLETE / NO-GO for trusting live signals until high-impact findings are fixed and verified.**

## RT-25 — MEDIUM: CRSD's live market-factor candle path is not wired to live index ticks

**Files/functions:** [`backend/streaming/market_stream_manager.py` — `start_stream`, token subscription and `on_ticks`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/market_stream_manager.py#L1191-L1291), [`on_ticks`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/market_stream_manager.py#L1523-L1547), [`_on_candle_close`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/market_stream_manager.py#L1311-L1369); [`backend/streaming/crsd_live_runtime.py`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/streaming/crsd_live_runtime.py#L65-L94); [`backend/strategy/crsd_strategy.py` — `CRSDPeerContext.market_bar` and `_ingest`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/strategy/crsd_strategy.py#L117-L133).

**Evidence:** `MarketStreamManager` builds `MultiSymbolCandleAggregator` with `resolved_token_to_symbol`, the equity-universe token map only. In `on_ticks`, tokens in `resolved_index_token_to_symbol` are sent to `ssf_one_minute_runtime.on_tick(tick)` and immediately `continue`, so index ticks do not enter `cash_ticks` and are not passed to that aggregator. The only call to `crsd_live_runtime.update_market_candle()` is inside `_on_candle_close`, the callback of this equity-only aggregator. `CRSDStrategy._ingest()` requests `ctx.market_bar(ts)` for the exact target candle timestamp and fails closed when the current market bar is absent.

**Failure scenario:** CRSD initializes from a historical/cached NIFTY frame, then live stock candles arrive after the end of that frame but new NIFTY candles are never appended through this route. Exact-timestamp market bars become unavailable; CRSD repeatedly resets/declines signals despite the equity ticker being connected.

**Recommended fix:** Route index ticks through a separately typed index candle aggregator (or another verified path) that constructs completed bars aligned to the same 15-minute grid and calls `update_market_candle`. Do not pass raw index ticks as completed bars and do not reuse the equity symbol map. Add an integration test proving a live NIFTY candle at timestamp T reaches CRSD context and is queryable at exactly T, while missing/stale market bars remain unavailable.

## RT-26 — MEDIUM: backtest position sizing silently ignores capital, stop distance and margin inputs

**Files/functions:** [`backend/backtest/strategy_backtester.py` — `PositionSizer.calculate_order_quantity`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/backtest/strategy_backtester.py#L112-L130) and its caller around lines 260–268; [`backend/backtest/pair_backtester.py` — `PairPositionSizer.calculate_pair_quantities`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/backtest/pair_backtester.py#L39-L104).

**Evidence:** The single-instrument sizer accepts `capital`, `stop_distance`, `available_margin`, `estimated_price` and `enforce_max_risk_cap`, but returns one unit for equity or at least one configured lot for futures. The pair sizer fixes target quantity to 100 and reports `total_risk_allocated=0.0`, `risk_scale=1.0`, irrespective of its capital, stop-distance and available-margin inputs.

**Impact:** This invalidates any interpretation that P&L or drawdown responds to supplied capital/stop-based sizing or margin constraints. It does not prove the live signal engine places orders; this finding concerns the offline research/backtest quantity model.

**Recommended fix:** Keep the product's agreed risk scope intact. Either model historically valid fixed quantities explicitly and remove/rename unused sizing parameters, or implement a transparent research sizing model with assumptions shown in every report. Tests must prove reported quantities and P&L match the stated model for equity, futures and hedged pairs.

## RT-27 — MEDIUM: performance metrics omit no-trade sessions and can use the wrong CAGR duration

**Files/functions:** [`backend/backtest/strategy_backtester.py` — `run`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/backtest/strategy_backtester.py#L179-L181), [`backend/backtest/pair_backtester.py` — `run`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/backtest/pair_backtester.py#L132-L138), [`backend/backtest/performance.py` — CAGR and daily returns](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/backtest/performance.py#L89-L128).

**Evidence:** The ordinary `run()` paths call `PerformanceAnalyzer.generate_report(trades, initial_capital=...)` without the requested backtest start/end dates or all trading dates. When omitted, the analyzer uses the first trade's entry time and last trade's entry time for CAGR, and its fallback daily return series includes only dates on which a trade occurred.

**Impact:** CAGR can be annualized over a shorter interval than the requested test window, while Sharpe can omit no-trade sessions. Comparisons across strategies with different trade frequencies can therefore be misleading.

**Recommended fix:** Pass validated source-data boundaries and the full included NSE trading-session calendar to the report generator. Keep zero-return days. Add regression cases where trades occur only in the middle of the requested period and where several valid sessions have no trades.

## RT-28 — LOW: undefined and infinite profit factor are serialized as the same finite value

**File/function:** [`backend/routes/backtest.py` — `_safe_pf`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/routes/backtest.py#L68-L73).

**Evidence:** `_safe_pf()` returns `99.9` both for `float('inf')` and for NaN (`pf != pf`). An infinite profit factor may represent positive wins and no losses; NaN means the metric is undefined/invalid. The route erases that distinction.

**Recommended fix:** Serialize a JSON-safe nullable numeric value with an explicit status (`FINITE`, `INFINITE_NO_LOSSES`, `UNDEFINED`) and render the status distinctly. Do not invent a large finite number to stand in for either case.

## RT-29 — MEDIUM: an authentication test invokes real logout side effects

**Files/functions:** [`tests/test_api_auth.py`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/tests/test_api_auth.py#L41-L49), [`backend/routes/kite.py` — `kite_logout`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/routes/kite.py#L145-L158), [`backend/broker/kite_adapter.py` — `clear_session`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/broker/kite_adapter.py#L376-L398).

**Evidence:** The test directly calls the route-level logout function with the current configured secret. The route stops the singleton market stream and calls `clear_session()`, which deletes the configured session-token file when present. These dependencies are not mocked in the inspected test.

**Failure scenario:** Running the test suite while the developer has an authenticated local Kite session may stop the current stream and remove that token file. This can disrupt the development/runtime session and makes the test environment-dependent.

**Recommended fix:** Unit-test authorization with a test client and mocks for stream/session operations. Any test of file deletion must use a temporary isolated token path. Test that production side effects are called only in the explicitly mocked integration case.

## RT-30 — MEDIUM, conditional on unchanged example configuration: known placeholder shared secret is accepted

**Files:** [`.env.example`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/.env.example#L15-L17), [`backend/config/settings.py`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/config/settings.py#L308-L326), [`backend/security.py`](https://github.com/Krishna27-art/trading_algorithm/blob/c6a2a58a1937c97ed711ff259f97421b1170d977/backend/security.py#L16-L31).

**Evidence:** The example sets `APP_SHARED_SECRET='CHANGE_ME_GENERATE_A_REAL_SECRET'`. The dependency rejects an empty configured secret and compares the supplied header against the configured value, but no inspected startup validation rejects this known placeholder. Copying the example unchanged therefore makes the public placeholder the accepted credential for the routes using this dependency.

**Recommended fix:** Reject known placeholders at startup, require a sufficiently strong generated value, and do not bundle the server secret into a browser build. For deployment beyond localhost, replace this shared header with actual user/session authentication and authorization. The impact is conditional on the environment retaining the placeholder and on route reachability.

---

## Updated disposition after continuation

- Source commit is still `c6a2a58a1937c97ed711ff259f97421b1170d977`; these findings do not assert anything about uncommitted local files.
- Total source-level findings in this combined report: **30 (RT-01 through RT-30)**. Some are conditional on configuration/deployment, and some affect research/backtesting rather than the live signal pipeline; see each item for scope.
- Repository-wide file-by-file coverage is **not complete**. The cache corpus was inventoried as a group, and repeated/generated metadata was not treated as independent business logic. Many source files remain unreviewed or only partially inspected.
- No code was changed; no tests, local build, live Kite authentication, market-stream integration or stress test was run. The system remains **NO-GO for trusting actionable live signals** until high-impact live findings are fixed and verified.
