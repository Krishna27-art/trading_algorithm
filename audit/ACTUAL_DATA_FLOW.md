# Actual Data Flow (Provisional)

**Status: provisional map from inspected source only. The repository-wide call graph has not been completed.**

```text
Kite login UI/API
  -> backend/routes/kite.py
  -> backend/broker/kite_adapter.py
  -> session token file / in-memory Kite client

KiteTicker / authorized live stream
  -> backend/streaming/market_stream_manager.py
     -> validates token and exchange timestamp, ordering, positive finite LTP
     -> backend/streaming/live_market_state.py
     -> backend/indicators/volume_profile.py
     -> backend/data/candle_aggregator.py
          -> completed candle callback
          -> backend/streaming/live_signal_engine.py
               -> backend/strategy/prediction_service.py
                    -> actual registered strategies (all implementation wiring still to be traced)
               -> persistence/status snapshots

React
  -> frontend/src/api/client.js
  -> frontend/src/hooks/usePolling.js
  -> frontend/src/pages/LiveSignalsPage.jsx
       -> GET /api/stream/status
       -> GET /api/stream/signals
       -> GET /api/stream/market
  -> frontend/src/pages/DashboardPage.jsx consumes stream and health endpoints too

Separate research path
  -> frontend/src/pages/BacktestPage.jsx
  -> backend/routes/backtest.py
       -> cache path / backend/data/historical_loader.py
       -> backend/backtest/strategy_backtester.py or backend/backtest/pair_backtester.py
       -> backend/backtest/performance.py
       -> research metrics return to the frontend
```

## Verified boundaries / known cautions

- Live signals page uses the stream-backed API path and is documented in code as separate from `/api/research/live`.
- Market quote route source explicitly represents unavailable values as `None` rather than substituting LTP for missing close/open/VWAP.
- Scanner's `allow_synthetic` default is false; an unauthenticated call raises instead of returning synthetic candidates unless a caller explicitly opts in.
- The backtest path's cache entry point has a missing expected-instrument/range validation step (RED-002).
- The default NIFTY backtest retains NFO/FUTURES configuration but gets the NSE index token (RED-003).
- The stream and route integration has only been traced through selected ranges; this is not a complete graph of every possible REST/stream/research/legacy path.


## Continued trace: data-validation and lifecycle details

- `PredictionService._build_prepared()` requires a present, positive `vwap` column and rejects today's missing 15-minute slots rather than filling them. That decision applies to the shared prepared data before the per-strategy evaluators run, so one current-session candle gap can make all eight per-symbol strategy outputs unavailable for that evaluation. Prior-session continuity issues make warm-up-dependent strategies report `WAITING`.
- `PredictionService` evaluates eight strategy outputs, but `LIVE_CONSENSUS_STRATEGIES` deliberately contains only seven single-name directional systems. CRSD is evaluated and displayed separately because it is a pair/relative-value strategy; its not appearing as a single-name consensus vote is intentional.
- The dashboard/live-signal UI polls the stream status, signal payload and market state independently. Backend per-symbol timestamps and top-level feed freshness are both checked; this is a positive guard, but it does not replace runtime end-to-end verification.
- Research reporting path has separate issues: cache metadata is not request-bound in the backtest route; default NIFTY index token/config identity is mixed; and ordinary report calculation omits no-trade days from Sharpe inputs.
- Stream startup takes the exclusive filesystem lock before validating all startup prerequisites. The reviewed early exception branches do not release it transactionally.
- A public tracked SQLite database exists, but its binary contents were not inspected. Treat its privacy status as unknown, not safe or unsafe, until a read-only content review is performed.


## Continuation finding: CRSD market-index data branch (source commit `c6a2a58a1937c97ed711ff259f97421b1170d977`)

The broad live flow above is not sufficient to assert all instruments flow through one candle aggregator. The source shows this split:

```text
KiteTicker on_ticks
  ├─ cash/equity token -> cash_ticks
  │    -> VolumeProfileEngine.process_ticks
  │    -> MultiSymbolCandleAggregator.process_ticks
  │         -> _on_candle_close
  │              -> live market state
  │              -> crsd_live_runtime.update_stock_candle
  ├─ index token -> ssf_one_minute_runtime.on_tick -> continue
  └─ futures token -> ssf_context_store.update_futures -> continue
```

`MultiSymbolCandleAggregator` is created with the equity-only `resolved_token_to_symbol` map. The index branch is handled separately and does not enter `cash_ticks`. Yet `crsd_live_runtime.update_market_candle()` is called only within `_on_candle_close`, and CRSD later requires an exact-timestamp market bar. This is a source-evidenced wiring mismatch; runtime reproduction was not performed.

## Trace confidence boundary

This is a provisional trace of the canonical path and selected connected paths. It is not a complete call graph for every tracked file or every legacy/research path. Index/futures handling, database lifecycle, scanner cache identity and all strategy-specific state transitions still require individual path-level and runtime verification.
