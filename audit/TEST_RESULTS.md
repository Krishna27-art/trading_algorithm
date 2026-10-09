# Test and Validation Results

**Overall status: INCOMPLETE. No source files or remote repository files were modified.**

## Verified in this audit

| Check | Status | Evidence / limit |
|---|---|---|
| GitHub tree inventory | Observed | 1,187 tracked files; 1,219 tree entries; tree response not truncated. |
| Audited commit | Observed | `c6a2a58a1937c97ed711ff259f97421b1170d977` (`updated`), Oct. 9, 2026 00:44:18 UTC. |
| 700-stock JSON structure | PASS — static parse only | 700 rows, 700 unique symbols, category counts 100/100/500, required fields in parsed rows passed simple checks. This does not establish current NSE membership or cap ranking. |
| Selected order-call scan | NO MATCH IN TARGETED FILES | No `place_order`, `modify_order`, `cancel_order`, `place_gtt`, `modify_gtt`, or `delete_gtt` matches in 12 inspected core broker/route/engine/strategy/database files. Not a repo-wide or runtime guarantee. |
| GitHub Actions runs | NO DATA | Endpoint returned zero workflow runs. No test pass is inferred. |
| CRSD market-bar flow | SOURCE DEFECT FOUND | The index-tick branch bypasses the equity aggregator; market-candle update callback only exists on the equity aggregator path. No runtime test was run. |
| Backtest position/metric paths | SOURCE DEFECTS FOUND | Fixed quantities, incomplete metric date inputs, and infinity/NaN profit factor conflation traced in source. |

## Not run / unverified

- Full `pytest` suite, Python compile/import/lint/type checks.
- Frontend dependency install, test, lint or production build.
- Local working tree status or uncommitted file changes.
- End-to-end Kite OAuth, API token resolution, live exchange tick timestamps, stream reconnect or live signal generation.
- Any current market-hours, 700-symbol opening-load or 10x tick-volume stress test.
- Current official exchange-list membership validation for the 700-stock universe.
- Tracked SQLite binary contents; its privacy status remains unknown.
- Exhaustive line-by-line inspection of every first-party source/test/frontend/config file. Coverage ledger explicitly contains IN_PROGRESS / NOT_REVIEWED group rows.

The local repository checkout/archive was not available through this environment, and direct container network lookup failed DNS resolution. No test/build results are claimed. No broker authentication or trading endpoint was called. Do not run `tests/test_api_auth.py` against an active development session without isolation: the inspected logout test calls real logout logic that can stop the stream and delete the configured session-token file.

## Regression tests required before approval

1. Expired directional predictions must be withheld and consensus recomputed at API serve time.
2. A completed live NIFTY 15-minute bar must reach CRSD's exact-timestamp market context; stale/missing bars must fail closed.
3. Wrong-token, wrong-interval, mismatched-symbol and insufficient-range caches must be rejected by all consuming paths.
4. Universe corruption, duplicate symbols/tokens and category-count mismatch must block stream startup.
5. Missing order-book depth must not pass the spread/liquidity eligibility filter.
6. Failed stream startup must release file locks and clean partial resources.
7. Book-queue saturation must process the latest pending snapshot or expose explicit dropped/degraded status.
8. Volume-profile late starts, stream gaps and date rollover must never produce complete profiles or mixed-session files.
9. NIFTY backtest must use a matching index or actual dated futures instrument, token, lot size and cost model.
10. Position quantity assumptions and performance date windows must match the reports; zero-trade sessions must be represented in daily metrics.
11. Profit factor must distinguish finite, no-loss/infinite and undefined values.
12. Authentication tests must mock logout/session deletion and reject known shared-secret placeholders.
13. Run entire tests in a disposable checkout with a temporary token path and no live broker session, then record commit, commands, counts, failures and skips.
