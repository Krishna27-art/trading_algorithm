# Strategy Improvement Plan

Baseline: main at efd3cc831e24624724fa89fc706bc0a497db8770. Bug fixes and strategy enhancements must remain distinct. First fix only defects supported by code; do not loosen thresholds merely to create more signals.

## Required correctness changes

| File / strategy | Change | Classification | Expected benefit | Regression risk | Tests |
|---|---|---|---|---|---|
| backend/strategy/sector_impulse_strategy.py | Group lag shifts and rolling sums within each trading session; require a complete rolling window. | Required bug fix | Stops cross-session samples contaminating lag and volatility estimates. | Some model fits may become ineligible with insufficient within-day bars; this is correct fail-closed behavior. | Two-session boundary fixture, lag pairs, low-sample/constant-series cases. |
| Sector Impulse | Keep candle-open timestamp for peer/index lookup but use bar-completion time for entry window, square-off and output timestamps. | Required timing fix | Signals match the first time the close is knowable and prevent future peer lookups. | Entry decisions change at time-window edges. | 09:30→09:45 unit test; exact peer lookup still uses 09:30. |
| backend/strategy/crsd_strategy.py | Separate raw source time from event time for entries/exits/output, preserving raw time in _ingest. | Required timing fix | Prevents 15-minute early signals while retaining exact peer matching. | Accidentally shifting _ingest timestamps would break context alignment. | Entry/exit boundary, raw peer timestamp and output timestamp tests. |
| CRSD | Avoid nested same-quote f-string syntax requiring Python 3.12 grammar, or declare Python >=3.12. | Compatibility fix | Avoids module import failure on older Python if those runtimes are supported. | Preserve exact reason text. | Import on declared versions. |
| backend/strategy/prediction_service.py | Missing persistent SSF runtime must return UNAVAILABLE; never instantiate a blank strategy from inside the evaluator. | Required reliability fix | Missing state is visible instead of being presented as a normal evaluation. | Direct callers now explicitly need the proper owner/runtime. | None-runtime test; owner-symbol mismatch; live runtime wiring test. |
| backend/strategy/ssf_l5_srm_strategy.py | Make an exact repeated BookSnapshot idempotent before feature-state mutation, but allow changed depth data even at the same coarse timestamp. | Reliability improvement | Avoids duplicate rolling state updates when candle evaluation reuses the last book event. | Equality must cover all feature-relevant values; timestamp-only dedupe is unsafe. | Duplicate snapshot and changed-snapshot tests. |

## Conditional enhancements, not automatic changes

### ORB
Keep 30-minute opening range, close breakout and VWAP confirmation. Test range thresholds by ATR/price, gap direction, false break/re-entry, late-break expiry and relative-volume confirmation as separate ablations. A volume filter may suppress false breaks but can also reject valid early breakouts.

### CPR
Keep the textbook pivot formula and bottom/top normalization. Add tests for inverted raw BC/TC, incomplete prior sessions, gaps and invalid OHLC. Evaluate narrow/wide percentile lookback chronologically; avoid an absolute width threshold shared across differently priced stocks.

### Dual EMA
Keep EMA9/EMA21, SMA200 and ATR buffer until the original rules have a valid baseline. Test crossover timing, duplicate evaluation on unchanged input, warm-up sufficiency, sideways whipsaws, tick rounding and post-signal execution. ADX/volume/EMA-distance filters remain experiments until ablation proves improvement.

### APEX
Preserve the strategy's current meaning. Log active inputs/weights and test missing/stale inputs. Perform leave-one-component-out tests for gap, slot-relative volume, L2 imbalance, sector/market strength and catalysts; freeze weights before untouched evaluation. Do not create synthetic context to make the composite score complete.

### Sector Impulse
After core fixes, validate sector mapping, peer freshness, exact timestamp alignment, nonpositive/missing prices, lag stability and changing leader relationships. Never replace missing sector data with an unrelated proxy. Avoid tuning min correlation/lag/horizon on the test period.

### SSF-L5-SRM
Keep real L5 and fresh futures/sector context requirements. Recover original documented thresholds or clearly label numerical defaults as research parameters. Record feature timestamps and replay realistic depth updates, spread widening, missing OI/futures values, stale sector data and expiry windows. Test one-minute regime warm-up independent of L5 signal output.

### AOU-OSS
Add mathematical tests for OU half-life, boundary behavior, zero-volume VWAP windows, invalid bars, stale L2 and numerical solver convergence. Replay historical L2 if recorded; otherwise historical performance is not validated. Sensitivity-test anchor length, half-life, friction, stop multiple and volatility ratio using development data only.

### CRSD
Keep pair/basket signals separate from directional consensus. Validate peer overlap, exact-time alignment, factor condition numbers, in-training calibration, convergence after costs, individual-leg liquidity and partial fills. Calibration on its own training window is in-sample selection; pass/fail must rely on untouched chronological data.

## Acceptance criteria

1. New tests reproduce the original failure and assert a specific invariant.
2. Existing tests run unchanged; do not delete or weaken failures.
3. Registry, runtime owner, inputs, state, and output contracts are integration-tested.
4. Original/new performance comparisons use the same inputs, symbols, time period, timing and costs.
5. Only measured out-of-sample improvement can be called an improvement. Everything else remains a hypothesis.