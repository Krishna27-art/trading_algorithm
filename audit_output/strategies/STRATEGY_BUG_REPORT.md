# Strategy Bug Report — Original Code

**Baseline:** main at efd3cc831e24624724fa89fc706bc0a497db8770. References below apply to that source snapshot before changes on the audit branch.

## Finding 1 — Sector Impulse calibration crosses session boundaries

- Severity: HIGH
- Confidence: CONFIRMED
- Location: backend/strategy/sector_impulse_strategy.py:153-174; function SectorImpulseStrategy._fit.
- Observed: Daily returns are computed with groupby(day_key.values).diff().dropna(), removing the overnight-spanning first return of each day. The joined rows are then processed with global res_l.shift(k), res_l.rolling(h), res_i.rolling(h) and rets["market"].rolling(h).
- Root cause: These global shift/rolling operations lose the session grouping after return differencing.
- When: Any training history covering more than one session.
- Impact: A lag pair or multi-bar volatility sample can combine yesterday’s last return with today’s first return. That contaminates leader/stock lag estimation, lead-lag stability and horizon/market volatility. It can change whether the model qualifies and the thresholds it derives.
- Scope: Sector Impulse.
- Fix: Group shifts by session and compute rolling windows independently within each session, with a full window required.
- Test: A two-session fixture with an extreme first return on day two must not use day one’s final value in either rolling sum or lag pair.
- Unknown: Historical count of affected signals; no market backtest was run.

## Finding 2 — Sector Impulse treats candle-open time as event time

- Severity: HIGH
- Confidence: CONFIRMED
- Location: backend/strategy/sector_impulse_strategy.py:214-252 and _manage near 254-278; input provenance in backend/strategy/prediction_service.py:_candle_dict/_replay_rows near 967-986.
- Observed: The common replay path preserves candle datetime; Sector Impulse reads candle datetime for its entry window and returns it in StrategySignal. Unlike ORB, CPR, Dual EMA, APEX and AOU-OSS it does not convert the 15-minute candle-open timestamp to completion time.
- Root cause: Data/context time and decision/event time use the same timestamp.
- Impact: Entry eligibility and signal timestamps can be one bar early; square-off timing can also be early/late relative to the close actually available.
- Fix: Retain the raw candle-open time for peer/index lookup to avoid look-ahead; use a separate completion time for entry windows, exits and returned signal timestamps.
- Test: 09:30 open timestamp must map to 09:45 completion; peer lookup must still use 09:30.
- Unknown: Live event timing was not executed.

## Finding 3 — CRSD treats candle-open time as event time

- Severity: HIGH
- Confidence: CONFIRMED
- Location: backend/strategy/crsd_strategy.py:1097-1158; input provenance in PredictionService._candle_dict/_replay_rows near 967-986.
- Observed: CRSDStrategy.on_candle uses candle datetime for its entry window, entry state and emitted signal. CRSDPeerContext matches peer/index records on exact source timestamps.
- Root cause: Event-time and peer-context timestamp are conflated.
- Impact: The strategy can mark a completed-bar decision as known 15 minutes before close. Simply shifting the input before _ingest would also break exact context alignment and risk using future peer observations.
- Fix: Keep normalized raw time for _ingest and exact peer lookup; use a derived completion timestamp for schedule, exit and public signal time.
- Test: Validate both the 09:30→09:45 event conversion and the exact 09:30 peer-key lookup.
- Unknown: Pair/basket execution and live context alignment were not run.

## Finding 4 — SSF evaluator can instantiate a blank stateful strategy

- Severity: MEDIUM
- Confidence: CONFIRMED when persistent runtime is absent
- Location: backend/strategy/prediction_service.py:_evaluate_ssf_l5_srm, approximately lines 2559-2569 in baseline.
- Observed: If ssf_strategy is None, the evaluator constructs a new SsfL5SrmStrategy(signal_only=True) instead of immediately failing unavailable, even though the surrounding comment says state is owned by the stream manager and a fresh instance has no regime/history state.
- Impact: Missing runtime wiring may look like a completed but neutral evaluation, with no warmed regime/feature history. The normal live candle caller does retrieve a persistent strategy in live_signal_engine.py:891-898, so this is conditional—not proof that every live call is affected.
- Fix: Return STATE_UNAVAILABLE when the persistent per-symbol runtime was not supplied.
- Test: With book validation stubbed valid, a None runtime must return UNAVAILABLE without constructing/advancing a new strategy.

## Finding 5 — CRSD signal reason contains Python-version-dependent nested f-string syntax

- Severity: MEDIUM
- Confidence: HIGH-CONFIDENCE, version-conditional
- Location: backend/strategy/crsd_strategy.py:1163-1165 in baseline.
- Observed: An inner f-string uses the same quote delimiter around st['z_cs'] that its expression needs.
- Root cause: This form relies on Python 3.12’s relaxed f-string grammar (PEP 701). requirements.txt does not declare a minimum Python version.
- Impact: On Python 3.11 and earlier, importing/parsing CRSD can fail. Since the prediction service imports CRSD, impact could extend beyond CRSD itself.
- Fix: Compute the display string separately or use format() without nested f-string parsing.
- Test: Import/compile on each supported Python version.
- Unknown: The deployment interpreter version is not defined in the repository.

## Finding 6 — Same SSF snapshot can be consumed by both book and candle evaluation paths

- Severity: MEDIUM
- Confidence: HIGH-CONFIDENCE
- Location: backend/streaming/live_signal_engine.py:891-898 and 938-949, plus 1367-1372; backend/strategy/prediction_service.py:2577-2579.
- Observed: The live book-update handler calls the persistent SSF strategy for each depth snapshot. The candle evaluation later passes the latest BookSnapshot and same persistent strategy through PredictionService, which calls on_book_update again.
- Impact: If the last snapshot contents are unchanged, the same mid/OFI/microprice can update rolling feature state twice; the duplicate can also interact with direction suppression/reset logic. No measurement was possible.
- Fix: Make exact duplicate snapshots idempotent before any state mutation. Do not deduplicate solely by timestamp because multiple real depth updates can share a coarse exchange timestamp.
- Test: Refeeding identical BookSnapshot must not mutate rolling state; a changed snapshot must still be processed.

## Verified observations, not defects by themselves

- README lists seven strategies and omits CRSD, but registry/evaluator includes eight.
- CRSD is intentionally outside single-name directional consensus because it emits hedge legs; that is not missing registration.
- The common prepared-candle layer intentionally fails closed on invalid input, missing VWAP, stale data and intraday gaps. A global preparation failure can make all candle strategies unavailable for a symbol; validate upstream data/telemetry before loosening this safety behavior.
- A low signal count, WAITING or NO_TRADE state is not independently proof of a defect.

## Limitations

The source baseline was reviewed, but no local checkout, runtime tests, live feed or historical backtest was available. Other code may still contain defects; no claim of exhaustive market validation or zero remaining bugs is made.

## Resolution status on review branch

The following source changes were subsequently made on audit/strategy-production-review-2026-10-12. This status does not change the original-code evidence recorded above:

| Original finding | Source change | Test / validation status |
|---|---|---|
| 1. Sector Impulse cross-session lag and rolling contamination | Fixed by session-grouped lag and rolling calculations in backend/strategy/sector_impulse_strategy.py. | Regression test added; not executed. |
| 2. Sector Impulse event-time handling | Fixed by deriving a completion timestamp while retaining raw time for context lookup. | Timestamp and raw-context regression tests added; not executed. |
| 3. CRSD event-time handling | Fixed by separating event time from raw peer-context timestamp. | Timestamp and raw-context regression tests added; not executed. |
| 4. SSF blank runtime fallback | Fixed by returning UNAVAILABLE when the persistent runtime is absent. | Regression test added; not executed. |
| 5. CRSD nested f-string compatibility | Fixed by avoiding a nested f-string in the z_cs display expression. | Compile/import check not run. |
| 6. Duplicate SSF snapshot consumption | Exact-equality guard added before feature mutation; changed same-timestamp snapshots remain processable. | Regression test added; not executed. |

Seven regression tests have been committed to tests/test_strategy_timing_regressions.py. The environment did not provide a working local checkout/test runner; none should be described as passing. No test result, runtime result or market-performance result is inferred from source edits alone.
