# Strategy Inventory

**Baseline:** main at efd3cc831e24624724fa89fc706bc0a497db8770 (2026-10-09 15:19:38 UTC). This inventory describes the original source before remediation on this branch.

## Shared production path

backend/strategy/prediction_service.py defines the eight STRATEGY_KEYS and evaluates them through PredictionService.evaluate_symbol() and _evaluate_all(). The candle path first passes through _build_prepared(), which normalizes/validates 15-minute candles, checks supplied symbol/timeframe labels, excludes incomplete/future candles and fails closed on missing/invalid VWAP or same-session gaps. _replay_rows() passes prepared rows to candle strategies.

Shared contract: backend/strategy/base_strategy.py (BaseStrategy, StrategySignal, SignalAction). Live orchestration: backend/streaming/live_signal_engine.py and backend/streaming/market_stream_manager.py. Market context: backend/data/models.py, backend/data/sector_peer_manager.py and backend/streaming/live_market_state.py. CRSD uses backend/streaming/crsd_live_runtime.py; SSF uses backend/streaming/ssf_runtime.py.

| Strategy | Source file / entry points | Registration and invocation | Dependency and test evidence |
|---|---|---|---|
| ORB — Opening Range Breakout | backend/strategy/orb_strategy.py; ORBCalculator.calculate_opening_range, IntradayORBStrategy._calculate_opening_range, _entry_signal, on_candle, on_tick, _check_active_position_exits | Registered as orb; PredictionService._evaluate_orb → _replay_orb → _replay_rows | Prepared OHLCV/VWAP and InstrumentConfig. tests/test_orb_strategy.py exists. Entry requires completed opening range, close beyond boundary and VWAP confirmation. |
| CPR — Central Pivot Range | backend/strategy/cpr_strategy.py; _compute_pivots, seed_context, _generate_entry_signal, on_candle, _check_active_position_exits | Registered as cpr; PredictionService._evaluate_cpr → _replay_cpr → _replay_rows | Previous-session H/L/C and prior CPR-width history. No dedicated test_cpr_strategy.py was found in the listed test tree; generic coverage is not ruled out. |
| Dual EMA | backend/strategy/dual_ema_strategy.py; _compute_indicators, seed_context, _combined_bars, _current_indicators, _build_entry_signal, on_candle and exit helpers | Registered as dual_ema; PredictionService._evaluate_dual_ema → _replay_dual_ema → _replay_rows | Prior sessions are required for EMA9/EMA21, SMA200 and ATR14. Related suites include tests/test_dual_ema_display.py and tests/test_live_strategy_independence.py. |
| APEX | backend/strategy/apex_engine.py; _validate_config, set_context, seed_context, _calculate_features, _generate_entry_signal, on_candle and exit helpers | Registered as apex; PredictionService._evaluate_apex constructs strategy context. ApexStrategy aliases ApexAivemStrategy. | Prepared candles and optional order imbalance, market/sector context, catalyst score and regime/VIX data. tests/test_apex_engine.py exists. |
| Sector Impulse | backend/strategy/sector_impulse_strategy.py; seed_context, reset_session, _fit, _features, on_candle, _manage | Registered as sector_impulse; PredictionService._evaluate_sector_impulse builds peer context, seeds history and replays today’s rows | PeerContext plus sector/market/leader data. tests/test_sector_impulse_strategy.py exists; its header describes structural/plugin tests rather than edge validation. |
| SSF-L5-SRM | backend/strategy/ssf_l5_srm_strategy.py; on_candle, _features, _has_required_context, _blocked, on_book_update, on_tick | Registered as ssf_l5_srm; persistent strategy comes from ssf_live_runtime and is selected in LiveSignalEngine. | Real Level-5 book, futures LTP/OI, fresh stock/sector returns and 1-minute regime bars. tests/test_ssf_l5_srm_strategy.py and tests/test_ssf_one_minute_runtime.py exist. Source labels some numerical configuration values as placeholders. |
| AOU-OSS | backend/strategy/aou_oss_strategy.py; rolling VWAP helpers, GK volatility, _fit_ou_kendall, boundary solver, _update_model, _entry_signal and exits | Registered as aou_oss; PredictionService._evaluate_aou_oss uses per-symbol stateful runtime machinery | Completed 15-minute bars, relative-to-VWAP spread, OU/half-life estimate, GK volatility regime and fresh L2 book. tests/test_aou_oss_live_runtime.py exists. |
| CRSD — Cross-sectional residual spread | backend/strategy/crsd_strategy.py; build_crsd_context, CRSDPeerContext, _fit, _simulate, _ingest, on_candle, _manage | Registered as crsd; PredictionService._evaluate_crsd uses crsd_live_runtime. It is intentionally outside single-name consensus because signals include hedge legs. | Historical own/peer/index 15-minute bars, exact-time peer snapshots, basket estimates and costs/convergence gates. No CRSD-named file was found in tests/, tests/unit/, tests/backtest/ or tests/walkforward/. Generic integration coverage may exist. |

## Inventory discrepancy

The baseline README lists seven strategies and omits CRSD. The source registry/evaluator include all eight: orb, cpr, dual_ema, apex, sector_impulse, ssf_l5_srm, aou_oss and crsd. This is documentation drift, not evidence that CRSD is unregistered.

## Validation boundary

This is a source trace against the public GitHub snapshot. A local checkout/worktree status and Python environment were unavailable, and this inventory does not claim that tests, live Kite data or market returns were executed.