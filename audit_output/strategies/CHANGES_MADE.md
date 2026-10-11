# Changes Made

**Source baseline:** main at efd3cc831e24624724fa89fc706bc0a497db8770.  
**Working branch:** audit/strategy-production-review-2026-10-12.

## Confirmed bug fixes

| File | Change | Type | Evidence / downstream effect |
|---|---|---|---|
| backend/strategy/sector_impulse_strategy.py | Added session-local rolling sums with full-window warm-up; grouped leader lag shift by session; computed market/leader/stock horizon volatility without bridging sessions. | Required bug/math-context fix | Prevents cross-session samples contaminating lag and volatility estimation. Some contexts may now fail eligibility rather than use contaminated samples. |
| backend/strategy/sector_impulse_strategy.py | Added configurable candle-open to completion timestamp handling; preserved raw timestamp for peer/index lookups, while entry window, square-off, and signal timestamps use completion time. | Required timing fix | Aligns decisions with completed-bar availability without changing peer lookup time. |
| backend/strategy/crsd_strategy.py | Separated the raw timestamp used by CRSD peer/index ingestion from the event/completion timestamp used for entry timing, position management, bookkeeping and signal output. | Required timing fix | Avoids a 15-minute-early signal label while preserving exact peer timestamp alignment. |
| backend/strategy/crsd_strategy.py | Replaced nested quote-conflicting f-string formatting with a format() expression. | Compatibility fix | Removes dependence on Python 3.12's relaxed f-string parser for that line; project-wide minimum Python version remains unspecified. |
| backend/strategy/prediction_service.py | Missing persistent SSF strategy now returns UNAVAILABLE instead of constructing a new blank SsfL5SrmStrategy instance. | Required reliability fix | Makes missing stateful-runtime wiring explicit. Normal live flow is expected to supply its per-symbol runtime. |
| backend/strategy/ssf_l5_srm_strategy.py | Added an early return for an exactly equal prior BookSnapshot before feature-state mutation. Same-time snapshots with changed content remain processable. | Reliability / idempotency fix | Prevents duplicate feature updates when the latest book snapshot is revisited by candle-level evaluation. |

## Tests added

File: tests/test_strategy_timing_regressions.py

1. Session-local rolling window must not bridge sessions.
2. Sector Impulse completion timestamp conversion.
3. CRSD completion timestamp conversion.
4. Missing SSF persistent runtime returns UNAVAILABLE.
5. An identical SSF snapshot is ignored, but changed same-timestamp depth is consumed.

**Execution status:** Tests were added remotely but have not been run in this environment. No passing status is claimed. The existing suite, Python compile/import checks, live Kite test and historical performance test remain unexecuted.

## Explicitly not changed

No ORB, CPR, Dual EMA, APEX or AOU-OSS trading thresholds were tuned. No new filters were added to force additional signals. CRSD remains a multi-leg strategy, separate from single-stock consensus. No order-placement API, production backtest engine, portfolio subsystem or risk-management module was added. No trade was placed.

## Remaining risks

Snapshot equality covers the BookSnapshot dataclass fields; the new duplicate guard and live call path still need execution tests. Historical strategy performance, fill quality and live-market readiness remain unproven.