# Before / After Comparison

## Comparison protocol

Original source: main at efd3cc831e24624724fa89fc706bc0a497db8770. Code correctness fixes are kept separate from optional strategy-rule changes.

## Original implementation

- Sector Impulse used global lag/rolling operations after day-grouped differencing and used candle-open timestamps for trading decisions.
- CRSD used the raw candle-open timestamp for entry timing and emitted signals; the nested f-string depends on Python 3.12+ parsing.
- SSF evaluator could construct a blank signal-only strategy when the persistent runtime was absent.
- The live book handler and candle evaluator can process the latest persistent SSF snapshot through the same stateful event method twice.

## Revised implementation

Pending implementation and targeted tests. No measured trading-performance comparison exists yet.

## Metrics

Original/revised win rate, expectancy, profit factor, drawdown or return figures are not available. Source correctness fixes must not be presented as measured trading improvements.