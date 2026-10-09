# File-by-File Repair Instructions — trading_algorithm

**Audited repository:** `Krishna27-art/trading_algorithm`  
**Audited commit:** `c6a2a58a1937c97ed711ff259f97421b1170d977`  
**Prepared:** 9 October 2026  
**Purpose:** Convert the static red-team findings into actionable coding-agent instructions.

## Read this before changing code

- The line references below refer to the audited commit above. Line numbers may shift if the branch has changed. Re-open the named function in the actual working tree before editing; do not blindly replace a line range.
- These are static findings, not proof that all runtime scenarios were reproduced. The earlier audit did not run the full test suite, frontend build, live Kite integration, or market-open stress test.
- The repository must remain a **signal-only** system: Kite real market data in, validated strategies and signals out, user places trades manually. Do not add order-placement, order-modification, cancellation, paper-trading, mock-market-data, synthetic production data, or fabricated indicators.
- Do **not** reintroduce the removed live risk module. For the backtest sizing issue, either label its fixed research assumptions honestly or make only a clearly scoped offline backtest-model correction.
- Missing, stale, mismatched, incomplete, or unverifiable required data must result in `UNAVAILABLE`/degraded/no-signal. It must not silently fall back to invented values or another instrument's data.
- Make small changes in the canonical code path. Remove dead/duplicate paths only after checking all callers. Do not create a second competing signal pipeline.
- Do not generate extra audit/summary `.md` files inside the repository. Tests may be added or updated. Updating an existing README is allowed where explicitly listed below.

## Severity order

1. **P0 — block misleading live signals/data:** RT-01, RT-02, RT-03, RT-04, RT-05, RT-06, RT-07, RT-25, RT-30.
2. **P1 — research/scanner correctness and data provenance:** RT-10, RT-12, RT-13, RT-14, RT-16, RT-22, RT-23, RT-26, RT-27, RT-28.
3. **P2 — lifecycle, access boundaries, tests and repository hygiene:** RT-08, RT-09, RT-11, RT-15, RT-17, RT-18, RT-19, RT-20, RT-21, RT-24, RT-29.

---

# RT-01 — CRITICAL: A stale directional signal can still appear live

**Files and audited locations**
- `backend/streaming/live_signal_engine.py` — `_with_current_freshness()` around lines 300–365; `get_all_predictions()` around 1620–1645.
- `backend/routes/stream.py` — `stream_signals()` around lines 125–174.
- `frontend/src/pages/LiveSignalsPage.jsx` — signal rendering around lines 165–215 and visible filtering around 195–210.

**What is wrong**
The API can refresh quote/feed freshness metadata without invalidating an old strategy direction. The stream route can return a symbol because its latest tick is fresh even when its saved prediction is old. The page uses signal direction/status and consensus without requiring the strategy result itself to be fresh. A fresh LTP is therefore capable of being shown next to an expired `LONG`/`SHORT` result.

**How it breaks**
If candle evaluation stalls or a queue item is dropped, ticks may continue updating the displayed price while the previous direction and consensus remain stored. The user can mistake “price feed is alive” for “signal was freshly evaluated.”

**Modify**
1. In `live_signal_engine.py`, enforce freshness at the moment predictions are serialized for the API, not only when they are first calculated. Use the existing canonical `PredictionService.revalidate_cached()` or one shared equivalent helper; do not implement different freshness rules in multiple files.
2. For each strategy result, validate source-candle timestamp, result/evaluation timestamp, required input timestamps, and configured maximum age. If a required timestamp is absent, future-dated, or too old, set status to `UNAVAILABLE` (or the project's existing equivalent) and remove/null the actionable direction. Include a reason and the relevant timestamp/age.
3. Recompute consensus **after** invalidating stale strategies. Only valid, fresh, directional strategy outputs may vote. `WAITING`, `NO_TRADE`, `UNAVAILABLE`, `ERROR`, `NOT_APPLICABLE`, and stale outputs must not count as votes.
4. In `routes/stream.py`, return the same canonical validated object; do not independently filter on fresh quote age and then serve cached directional predictions.
5. In `LiveSignalsPage.jsx`, keep `feed_freshness` and `signal_freshness` separate. Render the direction only when the strategy result is validated and current. Otherwise render a clear unavailable/stale state and never show the old direction or stale consensus as current.

**Delete / do not do**
- Delete/replace any rendering condition that treats a fresh tick or fresh LTP as proof that a strategy result is fresh.
- Do not simply hide the freshness badge while retaining the direction.
- Do not turn a vote ratio into a claimed calibrated probability.

**Create / update tests**
- Add a regression test where LTP updates but strategy evaluation is paused; old `LONG` must disappear/become unavailable.
- Add tests for an expired source candle, missing timestamp, future timestamp, and consensus being recomputed after one or more strategies go stale.
- UI/API contract test: a fresh quote plus stale prediction must never render a directional signal.

**Done when** no API or UI path can expose a stale directional result as a current signal.

---

# RT-02 — CRITICAL: Default NIFTY backtest mixes NSE cash-index candles with futures configuration

**Files and audited locations**
- `backend/routes/backtest.py` — `_resolve_instrument()` lines 105–135; `_load_history()` lines 135–184.
- `backend/data/instrument_resolver.py` — `CANONICAL_INDEX_TOKENS` lines 35–61; `resolve_token()` lines 190–238.
- `backend/config/settings.py` — default instrument settings around lines 10–32.

**What is wrong**
The default NIFTY path starts with an `NFO`/`FUTURES` configuration but resolves `NIFTY` through the `NSE` cash-index alias. The resulting historical candles can be cash-index data while the backtest treats them as futures prices and applies futures assumptions.

**How it breaks**
A report can look like a tradable futures backtest even though the data has no actual futures-contract basis, expiry, rollover, lot-size or matching contract-cost semantics. The report becomes unsuitable as evidence of futures strategy performance.

**Modify**
1. In `routes/backtest.py`, split the request/resolve paths into clearly different instrument types: (a) NIFTY 50 cash-index benchmark; (b) a dated NIFTY futures contract.
2. For futures mode, resolve an actual instrument-master row for exchange, tradingsymbol, expiry, token, lot size and tick size. Verify the resolved row is a future before downloading data.
3. Ensure the same resolved contract metadata controls history fetch, calculations, costs, quantity assumptions and response labeling. Do not resolve one instrument and then keep a different `InstrumentConfig`.
4. If the requested instrument is only the cash index, use an index-specific non-tradable benchmark configuration and label it clearly. Do not label it a real futures backtest.
5. Version the history/backtest cache key by instrument type/token/expiry so previously cached cash-index results cannot be reused as futures.

**Delete / do not do**
- Remove the branch that pairs the NSE cash-index token with an NFO futures configuration.
- Do not hardcode a current futures token or expiry.
- Do not delete broad historical data blindly; invalidate only entries whose identity is shown to be wrong.

**Create / update tests**
- Test that NIFTY cash index resolves to an index configuration and cannot be labeled futures.
- Test that futures mode fails closed if the instrument master has no valid dated futures contract.
- Assert the history token, instrument type, expiry and report label all refer to the same instrument.

**Done when** every backtest report clearly identifies the instrument whose bars were actually loaded.

---

# RT-03 — HIGH: A corrupt or incomplete universe is logged but still accepted

**Files and audited locations**
- `backend/config/universe.py` — `StockUniverse._load_and_validate()` lines 95–190; `resolve_700_universe_tokens()` lines 250–320.
- `backend/streaming/market_stream_manager.py` — `start_stream()` lines 1030–1145.
- Universe data: `backend/data/universe/700_stocks.json`.

**What is wrong**
Validation logs duplicates, malformed rows and count/category problems but can continue with available records. The stream's expected count is derived from that same possibly broken universe, so the loaded and resolved lists can agree even when both are incomplete.

**How it breaks**
A missing stock or bad category can silently reduce the live scanner/stream universe. The process may claim to have resolved the loaded list even though the required 700-stock composition was not met.

**Modify**
1. Validate required fields and types for every row: symbol, exchange, category/cap bucket, rank and any required instrument fields.
2. Enforce unique symbols and unique resolved tokens where the business contract requires uniqueness. Reject whitespace/empty symbols, invalid categories and non-integer/invalid rank values.
3. Define expected counts independently of the file being checked: exactly 700 total, 100 large-cap, 100 mid-cap, 500 small-cap if that remains the specified universe contract.
4. Make validation failures fatal for stream/scanner startup. Return a structured validation error with row index and reason; do not log-and-continue with a partial set.
5. Make token resolution compare resolved records against the separately validated expected symbol set. Unresolved required symbols must prevent a “fully ready” status.

**Delete / do not do**
- Delete the “Proceeding with available stocks” behavior for a required production universe.
- Do not make expected count equal to `len(loaded_records)`; that only validates a file against itself.
- Do not substitute proxy symbols for missing constituents.

**Create / update tests**
- Remove one row; duplicate a symbol; corrupt a category; use a malformed rank; return one unresolved token. Each must stop startup or produce an explicit unavailable state.

**Done when** the system cannot report a complete 700-stock universe unless the independent counts and symbol/token resolution have passed.

---

# RT-04 — HIGH: Missing bid/ask depth can still pass liquidity filtering

**File/location**
- `backend/scanner/liquidity_filter.py` — `_best_prices()` and `evaluate_stock()` around lines 70–190.

**What is wrong**
Spread validation runs only when `depth_data` exists. When depth is `None`, the code can continue through other filters and return `PASS` although no spread was calculated.

**How it breaks**
A quote response without actual bid/ask data can be ranked as liquid even though the required spread condition was never verified.

**Modify**
1. Make depth a required input to the spread check when that check is enabled.
2. Reject or mark `DATA_UNAVAILABLE` if depth is missing, empty, malformed, stale, has non-positive prices, has no usable best bid/ask, or is crossed (`best_bid >= best_ask`) unless the project explicitly models an auction/locked market as a separate state.
3. Calculate spread only from verified best bid and ask, with a documented denominator and units.
4. Keep “filter failed” separate from “filter could not be evaluated”; never let unavailable depth silently count as passing.

**Delete / do not do**
- Delete the path where `depth_data is None` implicitly skips the spread check.
- Do not synthesize a spread from LTP or daily range.

**Create / update tests**
Test `None`, missing keys, empty buy/sell lists, crossed market, malformed prices, stale depth and valid depth. Only the valid-depth case may pass the spread condition.

**Done when** no candidate passes a required liquidity/spread filter without real bid/ask evidence.

---

# RT-05 — HIGH: Shared API secret is exposed through the frontend build

**Files and audited locations**
- `.env.example` — lines 10–18.
- `backend/config/settings.py` — lines 290–335.
- `backend/security.py` — lines 12–32.
- `frontend/src/api/client.js` — lines 1–50.

**What is wrong**
`VITE_APP_SHARED_SECRET` is read by frontend code and can be embedded in browser JavaScript. Browser-delivered credentials are public. The server example also includes a known placeholder that is not clearly rejected at startup; RT-30 details that separate defect.

**How it breaks**
Anyone who can load the frontend can inspect its bundles/network requests and obtain the shared header value. If the backend is reachable, that value may unlock routes protected only by that shared secret.

**Modify**
1. In `frontend/src/api/client.js`, remove `VITE_APP_SHARED_SECRET` from the browser request-header design. Do not replace it with another `VITE_*` secret.
2. For local-only use, bind the backend to `127.0.0.1`/loopback and make that deployment boundary explicit.
3. If remote access is required, use a proper server-verified user/session authentication design, access control and secure session handling. Authentication must not rely on a universal secret shipped to every browser.
4. In `backend/security.py` and settings startup validation, reject empty/placeholder secrets for routes that still require a server-side secret. Treat this as defense-in-depth only, not as a substitute for actual remote user authentication.

**Delete / do not do**
- Remove frontend code and configuration that exports/sends a server secret.
- Do not put private auth material into Vite environment variables or localStorage/sessionStorage.

**Create / update tests**
- Frontend build/source test: no shared-secret value is compiled into client assets.
- API tests: unauthenticated calls fail for protected routes; accepted credentials are verified server-side; placeholder is rejected at startup (RT-30).

**Done when** no browser asset contains a reusable server-side secret.

---

# RT-06 — HIGH: An incomplete volume profile can be saved and treated as complete

**File/location**
- `backend/indicators/volume_profile.py` — `SingleStockVolumeProfile.process_tick()` / `to_snapshot_dict()` around lines 95–270; `save_session_profiles()` / `load_previous_session_profile()` around lines 340–515.

**What is wrong**
The first observed cumulative-volume reading becomes a baseline without proving capture began at the official session open. A profile with positive observed volume and no recorded gap can be labelled `COMPLETE`; a later loader validates level shapes but does not reliably reject degraded/gapped snapshots before setting the profile usable.

**How it breaks**
Starting the stream mid-session can miss the opening segment but still produce POC/value-area levels that are saved and accepted the following day as a complete prior-session profile.

**Modify**
1. Track the first observed tick time, coverage start/end, official session open/close, data source and explicit gap state.
2. Only mark `COMPLETE` if full required session coverage is proven or missing bars/ticks are reconstructed from a validated historical source. Positive volume alone is insufficient.
3. Persist a schema-versioned snapshot that includes `status`, `gap_detected`, coverage bounds and source identity.
4. In the previous-session loader, validate that the stored symbol/token/session date match; status is `COMPLETE`; no gap exists; coverage satisfies the required session window; levels and tick-size metadata are valid. Otherwise keep it degraded/unavailable and do not feed it into strategy rules.

**Delete / do not do**
- Remove the rule that equates “no gap flag + some volume” with complete coverage.
- Do not silently upgrade old snapshots without coverage metadata to `COMPLETE`.

**Create / update tests**
- Start a stream at 11:00: snapshot cannot be complete unless opening coverage is reconstructed.
- A `DEGRADED` snapshot or `gap_detected=true` cannot set `is_vp_allowed=True`.
- Test missing/incorrect session dates, bad levels and a valid fully covered prior session.

**Done when** partial data cannot enter a signal as an authoritative prior-session profile.

---

# RT-07 — HIGH: Stream-start response says started before the feed connects

**Files and audited locations**
- `backend/routes/stream.py` — start endpoint around lines 85–128.
- `backend/streaming/market_stream_manager.py` — `start_stream()` around lines 1030–1215 and connection callbacks in the same manager.

**What is wrong**
The manager may return `CONNECTING`, but the API response translates it to `started` without waiting for a confirmed `on_connect`/`CONNECTED` state.

**How it breaks**
Failed credentials or an asynchronous socket failure can still produce a success-looking start response, briefly misleading the UI/user about data availability.

**Modify**
1. Return the actual normalized manager state: `CONNECTING`, `CONNECTED`, `ERROR`, or the existing state equivalents.
2. Only report `CONNECTED` once the broker callback has confirmed it, not merely once a connect call was initiated.
3. Include a request/correlation ID and safe last-error/timeout fields in the response; do not expose secrets.
4. Update frontend state so “connecting” is distinct from “live/connected.” Poll the status endpoint for the transition.

**Delete / do not do**
- Remove the unconditional response status `started` when the manager has not confirmed a connection.

**Create / update tests**
Test successful callback, bad credentials, callback timeout, immediate disconnect and reconnect. The first HTTP response must not falsely claim connected.

**Done when** API and UI statuses reflect actual broker connection state.

---

# RT-08 — MEDIUM: Stream process lock is not released on every startup failure

**File/location**
- `backend/streaming/market_stream_manager.py` — `_acquire_process_stream_lock()` / `_release_process_stream_lock()` around lines 230–265; `start_stream()` around 1030–1205; stop cleanup around 1900–2005.

**What is wrong**
The cross-process lock is acquired before all dependencies have been validated, and some early exception paths do not guarantee release.

**How it breaks**
A stream start attempt made before login, with a missing token map or with a failed ticker initialization, can leave ownership held even though no usable feed exists. A different process can then be blocked from becoming owner.

**Modify**
1. Perform non-side-effecting validation first: active client, session, required token-map completeness, configuration and ticker prerequisites.
2. Acquire the process lock as close as possible to the point where this process becomes stream owner.
3. Structure startup cleanup using a single `try/except/finally` or explicit rollback helper so every failure path stops partially created components and releases ownership when this process acquired it.
4. Preserve correct behavior for an already-running stream and avoid releasing a lock owned by another instance.

**Delete / do not do**
- Remove duplicated exception cleanup that can diverge; centralize lock/ticker rollback.

**Create / update tests**
Inject failures after every startup stage and assert the lock is available, no ticker/worker remains partially active, and a later valid start can succeed.

**Done when** all startup failures leave the manager in a consistent and recoverable state.

---

# RT-09 — MEDIUM: Level-5 book queue can lose the last pending symbol update

**File/location**
- `backend/streaming/market_stream_manager.py` — book worker/queue setup around lines 25–55 and 280–340; `_on_book_update()` around lines 1405–1468.

**What is wrong**
The latest snapshot is saved in `_pending_book_snapshots`, but if queue insertion raises `queue.Full`, the exception is swallowed. If the queue later drains and the symbol has no further tick, its pending snapshot may never be scheduled.

**How it breaks**
A burst of L5 updates can leave a symbol with new pending depth that the SSF evaluator never consumes, leaving stale/absent book data without a clear degraded signal.

**Modify**
1. Implement a coalescing/dirty-symbol invariant: each pending symbol must either be queued, be actively processed, or be explicitly marked unavailable/dropped.
2. When the queue is full, add the symbol to a dirty set/counter instead of silently ignoring it.
3. When a worker frees capacity, drain/schedule dirty symbols until the pending set is covered; ensure symbols are not concurrently enqueued multiple times unnecessarily.
4. Track queue-full and maximum-age metrics and surface stale depth as unavailable to the strategy.

**Delete / do not do**
- Remove the bare swallowed `queue.Full` branch if it leaves the symbol without retry/reschedule or observable degraded state.

**Create / update tests**
Fill the queue, submit an update for symbol A, drain the queue, submit no more ticks for A, and prove A's latest snapshot is still processed or declared unavailable.

**Done when** queue saturation cannot silently strand an unprocessed latest L5 snapshot.

---

# RT-10 — MEDIUM: All volume-profile instruments default to a 0.05 tick size

**File/location**
- `backend/indicators/volume_profile.py` — `get_tick_size()`, `register_instrument()` and `initialize_universe()` around lines 270–330.
- Instrument metadata source/resolver must be checked for the authoritative tick-size field.

**What is wrong**
The volume-profile code defaults instruments to tick size `0.05` without resolving each instrument's permitted tick size. The tick value drives price rounding/bucket creation.

**How it breaks**
For an instrument whose actual tick increment differs, price-volume observations are binned incorrectly and POC/value area/level distances can be wrong even when every market tick was real.

**Modify**
1. Resolve `tick_size` from the official Kite instrument master/validated instrument record and pass it when registering each instrument.
2. Validate that the tick is finite and positive and that price rounding uses Decimal-safe or otherwise consistent arithmetic.
3. When tick size is unavailable or unverified, mark that profile unavailable; do not silently use a universal default.

**Delete / do not do**
- Delete the production fallback `0.05` for arbitrary symbols. A constant may remain only for a test fixture that names its instrument assumptions.

**Create / update tests**
Add instruments with different verified tick sizes and test bucket boundaries/rounding; missing metadata must produce unavailable status.

**Done when** every production profile states which validated tick size it used.

---

# RT-11 — HIGH, conditional on remote exposure: several market/data APIs have no authentication

**Files and audited locations**
- `backend/main.py` — router mounts/CORS around lines 80–106.
- `backend/routes/market.py` — around lines 75–145.
- `backend/routes/signals.py` — scanner around 150–205; journal/performance around 210–334.
- `backend/routes/stream.py` — live data around lines 125–230.

**What is wrong**
Selected control actions use a shared-secret dependency, while several routes returning market state, signals, scanner output, journal or performance data do not appear to have comparable route-level authentication. CORS limits browser behavior; it is not server-side identity or authorization.

**How it breaks**
If reachable over a network, arbitrary clients may read private signal/journal data or repeatedly trigger expensive 700-stock scanner/history work, consuming resources and broker API budget.

**Modify**
1. Decide and enforce the deployment mode. For strictly local operation, bind only to loopback and reject/flag remote bind configuration.
2. For remote access, apply server-verified authentication and authorization to every private route, not just stream controls. Protect journal/user-specific data according to ownership.
3. Rate-limit and bound expensive scanner/history/backtest parameters, including `top_n`, `days`, refresh behavior and concurrent requests.
4. Keep health/liveness endpoints minimal and separate from protected market/signal payloads.
5. Make frontend CORS configuration a secondary control, never the only access barrier.

**Delete / do not do**
- Do not treat allowed CORS origins or a shared browser secret as authentication.
- Do not silently expose private endpoints when the UI is served remotely.

**Create / update tests**
Enumerate API routes and test unauthenticated access for each private one; add rate/size-bound tests for expensive endpoints.

**Done when** the deployment has one explicit and tested authentication boundary for all private endpoints.

---

# RT-12 — MEDIUM: Holiday calendar treats any weekday in an unknown year as a trading day

**File/location**
- `backend/data/time_utils.py` — `HOLIDAYS_BY_YEAR` / `MarketCalendar.is_trading_day()` around lines 95–180.

**What is wrong**
The embedded holiday map has finite year coverage. For a year not present, a weekday is treated as a trading day by default.

**How it breaks**
Previous-session calculations and history warm-up can use an exchange holiday as if it were an open session, resulting in incorrect expected dates or false stale/missing-candle decisions.

**Modify**
1. Move holidays into a versioned, effective-dated calendar source that can be validated against official exchange dates.
2. Return an explicit `CALENDAR_UNAVAILABLE`/error when the evaluated date is outside verified calendar coverage. Do not silently infer exchange openness from weekday alone.
3. Keep session date and timestamps normalized to IST; distinguish weekend, holiday, special session and ordinary session where applicable.

**Delete / do not do**
- Remove the implicit `True` fallback for every weekday in an unknown year.

**Create / update tests**
Test known NSE holidays, weekends, year boundaries, missing calendar data and timezone conversion. Unknown-year dates must fail closed.

**Done when** an unverified calendar date cannot be treated as a verified trading session.

---

# RT-13 — MEDIUM: Scanner cache values are not consistently tied to requested symbol/token

**Files and audited locations**
- `backend/scanner/stock_ranker.py` — cache/context load around lines 430–625.
- `backend/data/historical_loader.py` — `load_cached_data_with_validation()` around lines 315–380.
- Cache sidecar metadata is also part of this validation boundary.

**What is wrong**
The historical loader validates OHLCV schema and timestamps, but the scanner cache path does not consistently verify that metadata identity (symbol, token, exchange, interval, source and dates) matches the requested security before using context.

**How it breaks**
A valid-looking CSV accidentally copied/renamed from another stock may pass data-shape checks and supply the wrong ATR/average-volume context for the current symbol's quote.

**Modify**
1. Make cache identity validation mandatory before calculating or consuming ranking context.
2. Compare metadata against the exact requested symbol, token, exchange, candle interval, source, schema version and requested start/end dates.
3. Validate row sessions/timezone and cache completeness. If the metadata is absent or mismatched, discard the cached context and fetch from Kite when allowed; otherwise report unavailable.
4. Use a cache key that includes instrument token/identity, interval and source; add a checksum/version to protect against sidecar/file mismatch.

**Delete / do not do**
- Remove fallback behavior that uses a correctly shaped but identity-unverified cache as if it belongs to the requested stock.
- Never substitute another ticker's history.

**Create / update tests**
Swap the cache files or sidecars for two stocks and verify both are rejected/refetched rather than cross-contaminating ranking.

**Done when** no cached history influences a symbol unless its instrument identity is proven.

---

# RT-14 — MEDIUM: Historical universe lookup can silently use today's constituents

**File/location**
- `backend/config/universe.py` — `get_universe(as_of=...)` around lines 15–48.

**What is wrong**
If dated membership data is missing or unusable, the helper can fall back to the current large-cap 100 list even when the caller requested a historical date.

**How it breaks**
A backtest for an older date may include current surviving constituents and exclude companies that were historically present, causing survivorship/membership bias.

**Modify**
1. If `as_of` is historical, require an effective-dated membership record covering that date.
2. Return explicit unavailable/error when historical membership cannot be proven.
3. Permit a current universe only for an explicitly current-universe request, with a response field identifying the universe date/source.

**Delete / do not do**
- Remove the silent fallback from historical `as_of` to today's list.

**Create / update tests**
Historical request + missing membership history must fail closed; current-date request may use current constituents but must label the source/date.

**Done when** historical membership is point-in-time or reported unavailable.

---

# RT-15 — MEDIUM: `.env.example` documents variables the application ignores

**Files and audited locations**
- `.env.example` — around lines 5–18.
- `backend/config/settings.py` — `AppSettings` and unknown-extra handling around lines 290–335.

**What is wrong**
`MAX_CAPITAL_PER_TRADE`, `MAX_DAILY_LOSS_LIMIT` and `SEMI_AUTOMATED_CONFIRMATION` are advertised without matching active settings consumers. Extra settings are ignored, so a user can change the values without changing system behavior.

**How it breaks**
Configuration appears active when it is not. In particular, these variables can imply risk controls or order-confirmation behavior that do not exist in the intended signal-only pipeline.

**Modify**
1. Because the live risk module has been removed, remove the obsolete `MAX_CAPITAL_PER_TRADE` and `MAX_DAILY_LOSS_LIMIT` entries from `.env.example` rather than reimplementing that module as part of this repair.
2. Remove `SEMI_AUTOMATED_CONFIRMATION` if no actual signal-only workflow consumes it; do not imply that live orders require confirmation when the app does not place orders.
3. Inventory every remaining variable in `.env.example`; each must have a typed `AppSettings` field and a real consumer, or be removed/explicitly identified as a deployment-only variable.
4. Prefer a startup warning or strict config validation for unrecognized keys so misconfiguration is visible.

**Delete / do not do**
- Do not reintroduce the live risk layer or order execution to make these example variables appear functional.

**Create / update tests**
Test that documented supported variables are parsed/consumed; obsolete variables are removed and no longer imply active behavior.

**Done when** every advertised configuration variable has a truthful, testable meaning.

---

# RT-16 — HIGH for derivatives research: NIFTY lot size is hard-coded to 75

**Files and audited locations**
- `backend/config/settings.py` — `DEFAULT_NIFTY_LOT_SIZE` around lines 10–16 and related consumers around 270–277.
- `backend/backtest/strategy_backtester.py` — futures quantity handling around lines 126–130.

**What is wrong**
A timeless setting assumes NIFTY lot size 75 even though the applicable contract specification changed for current relevant contracts. This affects derivative/research paths using the shared default; it does not prove every equity signal is affected.

**How it breaks**
Futures/options research quantity, notional, P&L or costs may be scaled incorrectly when the selected contract has a different lot size.

**Modify**
1. Resolve lot size from the selected instrument-master record for the exact contract/expiry.
2. If the data source does not provide it, use a documented effective-dated contract-specification source and validate its effective date. If neither is available, fail closed for quantity-dependent calculations.
3. Pass the resolved lot size through the backtest metadata and report it. Avoid hidden global defaults for current contracts.

**Delete / do not do**
- Delete the claim that a static value of `75` is always current. Do not simply replace 75 with another timeless constant.

**Create / update tests**
Test contracts before and after a lot-size effective-date boundary; confirm each contract uses its own metadata.

**Done when** the reported lot size is demonstrably linked to the selected contract.

---

# RT-17 — MEDIUM: Background stream workers lack explicit stop/join lifecycle

**File/location**
- `backend/streaming/market_stream_manager.py` — `_ensure_background_workers_started()`, `_flush_worker_loop()` and worker loops around lines 288–365; inspect related evaluation/history/book loops and shutdown handling.

**What is wrong**
Several daemon worker loops are indefinite and do not share an explicit stop event plus bounded join lifecycle. This does not prove a thread leak on every restart, but it weakens deterministic shutdown and ownership guarantees.

**How it breaks**
Shutdown or reload can stop the main stream while work is still in progress; late work can complete after a newer stream generation starts, or worker cleanup can be nondeterministic.

**Modify**
1. Add a manager-owned stop signal and have all manager-owned worker loops check it while waiting and before starting new work.
2. On shutdown, stop accepting new work, signal workers, drain/cancel queued work according to a documented policy, close the broker stream, then join worker threads with bounded timeouts.
3. Preserve stream-generation checks so old tasks cannot mutate state for a newer generation.
4. Make startup rollback use the same lifecycle cleanup rather than a separate inconsistent path.

**Delete / do not do**
- Do not rely exclusively on `daemon=True` as the shutdown mechanism.

**Create / update tests**
Repeated start/stop, failed startup and FastAPI shutdown must leave stable worker counts and no old-generation writes.

**Done when** all manager-owned threads have observable owners and deterministic shutdown semantics.

---

# RT-18 — MEDIUM: README quickstart and architecture refer to old paths

**File/location**
- Existing `README.md` — architecture/quickstart around lines 100–220.

**What is wrong**
Documentation refers to modules/scripts such as `backend/positions.py`, `backend/market.py`, `backend/stream_routes.py`, `backend/signals.py`, `backend/kite.py` and `run_algo.py` that do not match the audited current tree. “166+ tests passing” was not independently reproduced in the audit.

**How it breaks**
A developer may run the wrong command or inspect/update a dead path while the production entrypoint behaves differently.

**Modify**
1. Update the existing README architecture using actual current entrypoints: `backend/main.py`, `backend/routes/`, current streaming/strategy modules, and current React/Vite scripts.
2. Provide only commands that exist in the current package scripts and Python environment.
3. Replace unverified “tests passing” claims with the actual command and verified CI/local result only when a run has been performed at the referenced commit.
4. Keep the signal-only/manual-execution boundary explicit.

**Delete / do not do**
- Remove references to missing scripts/modules instead of creating obsolete compatibility files solely to satisfy stale documentation.
- Do not create a new documentation file; update the existing README only.

**Create / update tests**
Where feasible, have CI run the documented test/build commands so README commands remain reproducible.

**Done when** every documented path/command exists and has been verified.

---

# RT-19 — MEDIUM: SQLite database and generated cache metadata are tracked in Git

**Files/data**
- `backend/database/trading_system.db`.
- `backend/data/cache/` — tracked generated metadata sidecars (the audit inventoried 1,040 files in the cache tree; database content was not inspected).
- `.gitignore`.

**What is wrong**
Runtime database/cache artifacts remain tracked despite ignore rules. The audit did not inspect the database's contents and does not claim it contains private records; it is a review-required risk, not a confirmed leak.

**How it breaks**
Clones may inherit stale runtime state. Cache sidecars may refer to untracked/missing CSV payloads. If the DB contains unintended personal/session/trade data, it may have been published in repository history.

**Modify**
1. First inspect the DB locally and safely for schema/content. Back it up before any untracking/removal operation. Do not include tokens/secrets in logs or the report.
2. Decide whether this DB is intended sample data. If it is runtime state, add/confirm ignore rules and untrack it with `git rm --cached` rather than deleting a developer's working database automatically.
3. Remove generated cache files from Git tracking unless each is an intentional reproducible fixture; keep fixtures in a clearly separated test-data path.
4. Add a CI or pre-commit guard against new SQLite/session/cache files being committed unintentionally.
5. If sensitive information is found in Git history, assess revocation/rotation and history cleanup; deleting the current file alone does not remove history.

**Delete / do not do**
- Do not blindly `rm` the local database or valid local cache payloads.
- Do not assert the DB contains secrets before inspection.

**Create / update tests**
Test `.gitignore` and repository guard patterns; ensure clean startup can initialize a database and cache from controlled sources.

**Done when** only intentional, reproducible test/sample data is tracked.

---

# RT-20 — MEDIUM: Multiple Uvicorn workers split process-local stream state

**Files**
- `backend/streaming/live_market_state.py`.
- `backend/streaming/market_stream_manager.py`.
- Existing backend startup command/configuration.

**What is wrong**
Stream state, queues, predictions and the manager are process-local. The filesystem lock prevents multiple stream owners but does not share the owner's Python memory with other API workers.

**How it breaks**
Worker A receives Kite ticks while worker B handles a request from an empty or stale in-memory state. The response can look disconnected or signal-free even when another worker receives data.

**Modify**
1. For this architecture, simplest safe fix is to enforce one Uvicorn worker for the backend and reject/document unsupported multi-worker configuration.
2. If multi-worker operation is a real future requirement, move streaming ownership into one dedicated process and publish state via shared, process-safe storage/message transport. Do not attempt to share Python singletons across processes.
3. Update startup/deployment checks and the existing README command accordingly.

**Delete / do not do**
- Do not assume the process lock makes data shared between workers.

**Create / update tests**
Startup/deployment configuration with workers greater than one must fail clearly unless shared-state architecture is implemented.

**Done when** API requests always reach the canonical state owner, or unsupported worker counts are rejected.

---

# RT-21 — LOW/MEDIUM: Future-dated ticks can look fresh in aggregate health

**Files and audited locations**
- `backend/streaming/market_stream_manager.py` — `get_status()` around lines 1990–2075.
- `backend/routes/system.py` — health classification around lines 20–80.

**What is wrong**
An aggregate age calculation clamps negative age to zero. Other per-symbol checks may reject some future timestamps, so this is a consistency/observability defect, not proof that all strategy paths accept future ticks.

**How it breaks**
Bad source time or clock skew can make aggregate health report age zero/fresh while symbol/strategy checks reject the underlying data.

**Modify**
1. Preserve the sign of `now - tick_timestamp`; negative age must be classified as future-dated/clock-skewed, not zero.
2. Add an explicit `CLOCK_SKEW` or invalid timestamp state; make health degraded/unavailable until time validity is established.
3. Ensure timestamps are timezone-aware and normalized consistently; do not silently assign local time to naive timestamps without a documented source rule.

**Delete / do not do**
- Remove `max(0, age)`-style freshness clamping when it converts invalid future data into a fresh state.

**Create / update tests**
Test past, future, missing, naive and timezone-aware timestamps across both aggregate and per-symbol health.

**Done when** future timestamps never improve the system's freshness/health classification.

---

# RT-22 — HIGH: Volume-profile state is not reset on the next trading session

**File/location**
- `backend/indicators/volume_profile.py` — profile state, `process_tick()` and `initialize_universe()` around lines 80–350.

**What is wrong**
Existing profile instances retain their `session_date` and histogram. The engine routes next-day ticks into the existing profile instead of comparing the tick's normalized session date and creating a new per-session accumulator. A cumulative-volume reset may update the baseline but does not clear price-volume bins.

**How it breaks**
If the stream runs across sessions, today's volume may be mixed with yesterday's histogram, saved using yesterday's date/filename, contaminating POC/value area and potentially replacing prior data.

**Modify**
1. On every incoming tick, normalize exchange timestamp to the intended IST session date and compare with the profile's session date.
2. For a valid new session, finalize/save the prior profile once, create a new clean accumulator, reset histogram/gap/coverage state, and establish a documented volume baseline.
3. For same-day reconnect or out-of-order/late tick, preserve explicit degraded/gap semantics; do not represent missing data as recovered without proof.
4. Make snapshot filenames and session date derive from the profile's actual session, not a stale object's initial date.

**Delete / do not do**
- Remove any behavior that treats cumulative-volume reset alone as a full-session reset while retaining the old histogram.

**Create / update tests**
Consecutive sessions, overnight process survival, cumulative volume reset, reconnect gap, late/out-of-order tick, prior snapshot immutability and correct filenames.

**Done when** no histogram, coverage or date label crosses between trading sessions.

---

# RT-23 — MEDIUM: SSF-L5-SRM backtest does not test the live L5 strategy

**Files and audited locations**
- `backend/routes/backtest.py` — `_make_factory()` around lines 76–110.
- `backend/strategy/prediction_service.py` — live SSF evaluation around lines 2537–2635.

**What is wrong**
The backtest path uses 15-minute OHLCV and explicitly only evaluates the regime gate because historical order-book depth is absent. Live SSF requires fresh Level-5 depth and uses book updates. These are not identical strategy inputs or behavior.

**How it breaks**
A results table can be misread as verified production SSF-L5-SRM performance even though the replay did not exercise the central L5 behavior.

**Modify**
1. Mark current replay output as `REGIME_ONLY`/incomplete in both API metadata and UI; state that L5 execution logic was not tested.
2. Prevent this report from appearing directly comparable to a full backtest for other strategies.
3. Only enable a full SSF backtest when timestamp-aligned historical Level-5 data and the full live decision path can be replayed. If unavailable, return an explicit unsupported/incomplete result instead of inventing order-book values.

**Delete / do not do**
- Do not synthesize depth from OHLCV, LTP, or daily volume.
- Do not label a regime-only replay as a full SSF strategy backtest.

**Create / update tests**
Assert that OHLCV-only SSF backtest results carry an incomplete/regime-only status and cannot be marked as a full live-strategy replay.

**Done when** users can distinguish a partial gate experiment from a valid test of the production strategy.

---

# RT-24 — MEDIUM: Synthetic scanner mode remains callable from production scanner code

**File/location**
- `backend/scanner/stock_ranker.py` — `scan_universe(..., allow_synthetic=False)` and `_scan_synthetic()` around lines 341–465.
- `backend/routes/signals.py` / canonical scanner endpoint should be checked as callers; audited route passed `allow_synthetic=False`.

**What is wrong**
Production scanner code contains a seeded synthetic fallback behind `allow_synthetic=True`. The audited canonical endpoint explicitly passes false, so this finding does not establish that the current canonical API emits synthetic candidates; the unsafe fallback is still callable by another internal caller or future route.

**How it breaks**
A future call site can accidentally opt into generated ranking data when Kite is unavailable; a consumer might ignore the synthetic source marker and show it as real.

**Modify**
1. Remove `allow_synthetic` and `_scan_synthetic()` from the production scanner module, or isolate them into a test-only/research module that production imports cannot call.
2. Keep live failure behavior explicit: no valid quote/history means `UNAVAILABLE`/no candidate, with a source/reason field.
3. Search every call site for synthetic hooks and remove them from live routes/configuration.

**Delete / do not do**
- Delete seeded `seed=42` synthetic market ranking from production flow.
- Do not keep the path “just in case” for production outage handling.

**Create / update tests**
Repository-level tests should assert production scanner entrypoints cannot produce synthetic-ranked output under missing Kite credentials/data.

**Done when** the live scanner has no code path that manufactures market observations/candidates.

---

# RT-25 — MEDIUM: CRSD stops getting current NIFTY market candles from the live index stream

**Files and audited locations**
- `backend/streaming/market_stream_manager.py` — aggregator setup around lines 1191–1291; `_on_candle_close()` around 1311–1369; index/equity tick handling around 1523–1547; index/futures handling around 1667–1686.
- `backend/streaming/crsd_live_runtime.py` — `update_market_candle()` around lines 65–94.
- `backend/strategy/crsd_strategy.py` — `CRSDPeerContext.market_bar()` / `_ingest()` around lines 117–133 and 1006.

**What is wrong**
The configured candle aggregator receives the equity token map. Index ticks are handled in a different branch and immediately `continue`; the only observed `update_market_candle()` call is from the equity aggregator's candle-close callback. CRSD requires an exact-timestamp market bar for the same time as the stock bar.

**How it breaks**
CRSD may initialize with cached NIFTY candles, then run out of current index candles after the cache endpoint. Stock candles keep arriving but the matching fresh NIFTY bar does not, causing CRSD to fail closed/repeatedly produce no signal.

**Modify / create**
1. Add a dedicated, typed index-candle aggregation path aligned to the CRSD 15-minute interval. This can be a small new class/module if needed, or a separate aggregator instance using index tokens; choose the least duplicative design after checking existing aggregator capabilities.
2. Feed only completed, validated NIFTY candles to `crsd_live_runtime.update_market_candle()`. Do not pass raw ticks as completed candles.
3. Normalize candle timestamps to the same timezone/session boundary used by CRSD stock candles.
4. Handle missing/late index bars as unavailable; do not reuse a stale market bar for a newer stock candle.
5. Verify that the live index token set is explicit and distinct from the 700 equity universe token map.

**Delete / do not do**
- Do not rely on the equity-only aggregator to produce index candles.
- Do not proxy NIFTY with RELIANCE/HDFCBANK/TCS or another stock.

**Create / update tests**
Integration test: completed NIFTY bar at timestamp T reaches CRSD and can be retrieved via `market_bar(T)` for a stock candle at T. Missing/stale/misaligned market bars must remain unavailable.

**Done when** CRSD can receive current real NIFTY context continuously during live operation.

---

# RT-26 — MEDIUM: Backtest quantity code ignores several sizing inputs

**Files and audited locations**
- `backend/backtest/strategy_backtester.py` — `PositionSizer.calculate_order_quantity()` around lines 112–130; caller around 260–268.
- `backend/backtest/pair_backtester.py` — `PairPositionSizer.calculate_pair_quantities()` around lines 39–104.

**What is wrong**
The single-instrument sizer takes capital, stop distance, available margin and other sizing inputs but returns a fixed one-share equity quantity or one configured futures lot. The pair sizer fixes target quantity to 100 and does not use all supplied capital/margin/stop inputs as implied by its interface.

**How it breaks**
Offline P&L may not change in response to the quantity inputs a caller expects to control. Reports may be interpreted as capital-/stop-sensitive sizing when they are actually fixed-size research runs.

**Modify**
1. Keep this strictly in the offline backtest scope; do not add the removed live risk module and do not connect it to broker orders.
2. Decide whether these classes are intentionally fixed-size. If yes, remove misleading unused parameters, rename/document the fixed-size model, and state actual quantity in every report.
3. If genuine backtest sizing is required, specify a separate transparent research quantity formula and unit-test capital/stop/margin effects. Validate all assumptions and instrument contract metadata.
4. Ensure pair quantities and reported gross/net exposure match the executed simulated research quantities; no fabricated “risk allocation” fields.

**Delete / do not do**
- Remove unused parameters or misleading outputs; do not present a fixed 100-share pair as dynamic capital-based sizing.
- Do not reintroduce live trading/risk management.

**Create / update tests**
Test actual quantity values for equity/futures/pair cases and confirm report metadata matches the chosen fixed or dynamic model.

**Done when** the backtest's quantity behavior is explicit and its results cannot be misread as using inputs that are ignored.

---

# RT-27 — MEDIUM: Backtest performance excludes no-trade sessions and can use a wrong CAGR duration

**Files and audited locations**
- `backend/backtest/strategy_backtester.py` — `run()` around lines 179–181.
- `backend/backtest/pair_backtester.py` — `run()` around lines 132–138.
- `backend/backtest/performance.py` — CAGR/daily returns around lines 89–128.

**What is wrong**
Ordinary backtest callers do not pass the requested period boundaries or full included trading-session calendar into the analyzer. It can derive CAGR from first/last trade and build daily returns only from dates that contained a trade.

**How it breaks**
CAGR can use a shorter interval than the backtest request, and Sharpe-like calculations can omit valid zero-return/no-trade sessions. Results between strategies with different trade frequencies are not comparable.

**Modify**
1. Pass validated data start/end dates and the full verified market-session calendar to `PerformanceAnalyzer.generate_report()` from both backtest `run()` paths.
2. Build a daily equity/return series across every eligible trading session; a no-trade day is a zero-return observation unless another explicit model says otherwise.
3. Calculate annualization over the declared tested interval, not the interval between first and last trades.
4. Report actual data coverage separately from requested dates; if input history has gaps, disclose or reject the report.

**Delete / do not do**
- Remove fallback behavior that silently shrinks the test period to trade dates when requested boundaries are known.

**Create / update tests**
Test a period with trades only in the middle, multiple no-trade days and an empty-trade result. CAGR window and daily return count must reflect the validated test period.

**Done when** every performance metric's sample window is explicit and consistent across strategies.

---

# RT-28 — LOW: Infinite and undefined profit factor are both serialized as 99.9

**File/location**
- `backend/routes/backtest.py` — `_safe_pf()` around lines 68–73.

**What is wrong**
`_safe_pf()` converts both positive infinity and NaN to the same finite numeric value `99.9`, erasing the difference between no-loss/infinite profit factor and an undefined metric.

**How it breaks**
The frontend/report displays a fabricated finite number and cannot distinguish a valid edge case from an invalid/unavailable calculation.

**Modify**
1. Serialize valid finite profit factor as a number.
2. Represent infinite/no-loss profit factor and undefined/NaN separately using JSON-safe explicit status fields, e.g. `INFINITE_NO_LOSSES` and `UNDEFINED`, plus nullable numeric value.
3. Update the frontend renderer to show the status rather than coercing it to a misleading finite value.

**Delete / do not do**
- Delete the `99.9` substitute for Infinity/NaN.

**Create / update tests**
Test finite factor, no losses/infinity, undefined/NaN, missing trades and serialization through the API.

**Done when** the API reports the true metric state without inventing a number.

---

# RT-29 — MEDIUM: An auth unit test invokes real logout side effects

**Files and audited locations**
- `tests/test_api_auth.py` — around lines 41–49.
- `backend/routes/kite.py` — `kite_logout()` around lines 145–158.
- `backend/broker/kite_adapter.py` — `clear_session()` around lines 376–398.

**What is wrong**
The test directly calls the logout route with the configured secret. The route calls the global stream manager's stop function and `clear_session()`, which can delete the configured access-token file. The audited test did not isolate/mock these side effects.

**How it breaks**
Running a test suite while a developer's Kite session is active may stop their stream and remove the token file. Results also depend on developer environment state.

**Modify**
1. Use FastAPI's test client/dependency overrides for the HTTP authorization behavior.
2. Mock `market_stream_manager.stop_stream()` and the Kite session-clear function in the unit test.
3. Any dedicated test of token-file deletion must set the token path to a temporary directory/file created by that test.
4. Ensure test teardown restores global state and never targets the user's configured `.env` token path.

**Delete / do not do**
- Remove unmocked calls that trigger a real logout/stream stop during an auth-only unit test.
- Do not modify or delete a developer's token file to make tests pass.

**Create / update tests**
Run the auth test with a deliberately configured fake path and assert production resources are untouched; separately test the mocked logout route response.

**Done when** ordinary test execution cannot terminate the developer's feed or delete their local session.

---

# RT-30 — MEDIUM: Known placeholder shared secret is accepted if example config is copied unchanged

**Files and audited locations**
- `.env.example` — lines 15–17.
- `backend/config/settings.py` — around lines 308–326.
- `backend/security.py` — around lines 16–31.

**What is wrong**
The example secret `CHANGE_ME_GENERATE_A_REAL_SECRET` is non-empty and is accepted by the shared-secret comparison path; the audited startup path did not reject that known placeholder.

**How it breaks**
If an operator copies the example unchanged and exposes the API, the publicly known placeholder can satisfy routes using that shared-secret dependency.

**Modify**
1. Add startup/config validation rejecting the exact placeholder and common obvious placeholders (empty, `changeme`, `secret`, example strings). Do not log the secret itself.
2. Require a strong generated server-side value for any remaining local shared-secret route; provide an actionable startup error telling the operator to generate one.
3. Combine this with RT-05: remove browser-secret usage and do not claim this shared key secures remote user access.

**Delete / do not do**
- Remove the known placeholder as a usable default. `.env.example` may contain a blank key or a clear non-secret instruction, but a runnable public credential must not be present.

**Create / update tests**
Application settings/startup must reject the example placeholder and accept a generated sufficiently strong test secret. Protected route behavior must be tested without printing secrets.

**Done when** copying `.env.example` unchanged cannot start an API with the public placeholder as a valid credential.

---

# Final implementation order

1. Fix RT-01 and RT-25 signal/context freshness wiring first. Confirm that a stale/missing required context always means no actionable direction.
2. Fix RT-02 instrument identity and RT-03 universe fail-closed validation before relying on scanner/backtest outputs.
3. Fix RT-04 and RT-06 volume-profile eligibility; also fix RT-10 and RT-22 before using profile levels in strategies.
4. Fix RT-05/RT-11/RT-30 access boundaries before exposing the service beyond localhost.
5. Fix RT-07/RT-08/RT-09/RT-17 stream lifecycle and queues.
6. Fix RT-12/RT-13/RT-14/RT-16/RT-23/RT-26/RT-27/RT-28 research correctness and label semantics.
7. Fix RT-15/RT-18/RT-19/RT-20/RT-21/RT-24/RT-29 configuration, deployment, repository and test isolation.

# Definition of done for the coding agent

- Re-read the current source in every named file and verify the line ranges before editing.
- Produce a change list grouped into **MODIFY**, **DELETE**, **CREATE**. Explain any necessary deviation before implementing it.
- Preserve the signal-only architecture. Do not add order execution, broker order APIs, mock/synthetic market data, proxy instruments, or live risk-module code.
- Do not broaden scope into unlisted strategy rewrites or cosmetic UI redesign.
- Add tests beside the relevant existing tests; do not perform auth tests against a real configured Kite session.
- Run the smallest relevant tests after each fix, then the full test suite and frontend build if the local environment allows. State exact commands and results; never claim an unrun test passed.
- Review `git diff` to ensure no secrets, `.env`, token files, local DB content, unintended generated cache, or new audit Markdown files are committed.
- At the end report each RT ID as `FIXED`, `PARTIALLY FIXED`, or `NOT FIXED`, cite files/lines changed, and list tests actually run. Do not state that the system is production-safe unless every applicable acceptance condition was verified.
