# Test Results

**Baseline:** main at efd3cc831e24624724fa89fc706bc0a497db8770.  
**Review branch:** audit/strategy-production-review-2026-10-12.

## Execution status

| Check | Exact command / method | Status | Explanation |
|---|---|---|---|
| Establish original commit | GitHub API read of commits/main | PASS | Baseline SHA verified as efd3cc831e24624724fa89fc706bc0a497db8770. |
| Static inspection of eight strategy modules and shared call path | GitHub source reads | COMPLETED STATICALLY | All eight sources, registry, evaluation routing and relevant shared/runtime dependencies inspected. Not equivalent to local full test execution. |
| Existing full suite | pytest | NOT RUN / BLOCKED | No local checkout/dependency environment was available; clone could not resolve github.com in this execution environment. |
| New regression module | pytest tests/test_strategy_timing_regressions.py -q | NOT RUN / BLOCKED | Module is committed to the review branch, but no local project checkout was available to execute it. |
| Python compile/import checks | python -m compileall or targeted imports | NOT RUN / BLOCKED | No locally materialized repository/dependencies were available. |
| Baseline-vs-revised historical test | Existing research/backtest tools | NOT RUN | No verified historical candle/depth dataset was evaluated. |
| Live Kite verification | Authenticated running application | NOT RUN | No authenticated live session was run. |
| Broker order placement | None | NOT PERFORMED | Out of scope; no orders were placed. |

## Regression cases added, not run

File: tests/test_strategy_timing_regressions.py

1. Ensures a two-bar rolling window starts afresh each session.
2. Checks Sector Impulse maps a 09:30 candle-open timestamp to 09:45 completion time.
3. Checks CRSD maps a 09:30 candle-open timestamp to 09:45 completion time.
4. Checks a missing SSF persistent runtime returns UNAVAILABLE without creating a fresh instance.
5. Checks an identical BookSnapshot is ignored while changed contents with the same timestamp are processed.

These are regression-test definitions only. They are not passing test results until executed in the repository environment.

## Existing test files found

- tests/test_orb_strategy.py
- tests/test_apex_engine.py
- tests/test_sector_impulse_strategy.py
- tests/test_ssf_l5_srm_strategy.py
- tests/test_ssf_one_minute_runtime.py
- tests/test_aou_oss_live_runtime.py
- tests/test_live_strategy_independence.py
- tests/test_red_team_fixes.py
- tests/backtest/ and tests/walkforward/ suites

There is no dedicated test_cpr_strategy.py or test_crsd_strategy.py in the listed tree. Generic integration coverage is possible but has not been established by filename alone.

## Unknown results

No test exit codes, coverage percentages, execution timings, signal counts, win rates, expectancy, profit factor, drawdown, cost-adjusted returns or out-of-sample metrics were produced. These remain unknown.