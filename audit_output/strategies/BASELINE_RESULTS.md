# Baseline Results — Original Code

**Baseline commit:** efd3cc831e24624724fa89fc706bc0a497db8770 on main (2026-10-09 15:19:38 UTC).

## Repository state

- Remote branch/commit identified: main at efd3cc831e24624724fa89fc706bc0a497db8770.
- A local checkout was unavailable. The clone attempt could not resolve github.com, so local git status, Python dependencies, test database and local working-tree changes could not be inspected.
- Public source and test directory listings were inspected. No local changes were overwritten.

## Checks and outcomes

| Check | Method | Result |
|---|---|---|
| Baseline ref | Read GitHub commits/main | PASS: efd3cc831e24624724fa89fc706bc0a497db8770 identified |
| Strategy inventory | Inspect current source and registry | PASS: all eight expected modules and evaluator entries exist |
| Test inventory | List tests/, tests/unit/, tests/backtest/, tests/walkforward/ | PASS: known direct suites located; filenames alone do not prove coverage |
| Run test suite | pytest | NOT RUN: no local repository/dependency environment |
| Reproduce bugs | Unit/integration execution | NOT RUN: repro cases identified but unavailable to execute here |
| Measure strategy latency | Fixed representative input | NOT RUN |
| Historical strategy evaluation | Verified historical and depth data plus execution model | NOT RUN |
| Live Zerodha/Kite verification | Authenticated live session | NOT RUN |

No tests are marked as passed merely because test files exist or because an earlier report may claim results.

## Baseline trading metrics

Win rate, expectancy, average gain/loss, profit factor, drawdown, slippage-adjusted return, symbol/regime breakdown and out-of-sample performance are unavailable. Entering numeric values would be fabrication.

## What is established from source

- The shared preparation layer performs validation and deliberately fails closed on invalid/incomplete data, missing VWAP, stale inputs and intraday gaps.
- Sector Impulse global lag/rolling operations and timestamps, CRSD event timestamp/compatibility, and SSF missing-runtime handling present source-supported defects.
- Important SSF numerical thresholds are labelled placeholders in its own source.

## Required baseline work before performance claims

Run pytest on the exact baseline commit, preserve raw output and environment versions, add failing regression tests, and run baseline vs revised strategies on identical timestamped symbols and chronological periods. Use realistic post-signal executable prices and costs plus an untouched out-of-sample period. No profitability claim is justified at this stage.