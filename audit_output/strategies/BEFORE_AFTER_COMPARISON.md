# Before / After Comparison

**Original baseline:** main at efd3cc831e24624724fa89fc706bc0a497db8770.  
**Updated review branch:** audit/strategy-production-review-2026-10-12.

## Code correctness comparison

| Area | Before | After on review branch | Verification |
|---|---|---|---|
| Sector Impulse session windows | Global lag shift and rolling horizon sums could pair or roll across trading-day boundaries after daily return differencing. | Lead-lag shifts and horizon rolling sums are calculated within each session; a rolling sum requires a complete window. | Code changed; regression test added but not run. |
| Sector Impulse event time | Entry window and signal timestamp used raw candle-open timestamp. | Keeps raw timestamp for peer lookup, uses bar-completion time for schedule, exit and output. | Code changed; timestamp test added but not run. |
| CRSD event time | Entry window, bookkeeping and output used raw candle-open timestamp. | Keeps raw timestamp for ingestion; uses completion time for entry schedule, exits, bookkeeping and signal output. | Code changed; timestamp test added but not run. |
| CRSD interpreter compatibility | Nested f-string expression can fail parsing before Python 3.12. | Uses format() inside the outer f-string expression instead of a nested f-string. | Code changed; module compile/import not run. |
| SSF runtime absence | Evaluator could create a cold stateful instance if caller omitted persistent runtime. | Missing persistent runtime returns UNAVAILABLE. | Code changed; regression test added but not run. |
| SSF duplicate book event | Book update and candle-level evaluation can call on_book_update on the same latest snapshot. | Exact duplicate BookSnapshot is skipped before rolling feature state mutates; changed same-timestamp depth is still allowed. | Code changed; regression test added but not run. |

## Strategy logic changes

No discretionary strategy parameters, entry thresholds or trade-direction rules were deliberately tuned. These edits target timing, boundary correctness, compatibility and state integrity.

## Performance comparison

Not available. The original and revised versions have not been executed on matching historical market data. There are no measured before/after win rates, expectancy, profit factors, drawdowns, slippage-adjusted returns, trade counts or latency numbers.

## Conclusion

The code changes address specific source-level failure mechanisms. Tests have been written but not executed. This is not evidence that profitability or strategy quality improved, nor proof of production readiness.