import { apiGet, apiPost } from './client'

// POST /api/strategy/backtest?days=&symbol=&strategy= -> single-strategy report.
// Supports strategy = orb | cpr | dual_ema | apex | sector_impulse | ssf_l5_srm | aou_oss | crsd (pair backtest).
export const runBacktest = (strategy, symbol, days) =>
  apiPost(
    `/api/strategy/backtest?days=${days}&symbol=${encodeURIComponent(symbol)}&strategy=${encodeURIComponent(strategy)}`,
    undefined,
    { requireSecret: true },
  )

// GET /api/research/backtest?days=&symbol= -> ORB vs CPR vs Dual-EMA comparison
export const getResearchBacktest = (days, symbol) =>
  apiGet(`/api/research/backtest?days=${days}&symbol=${encodeURIComponent(symbol)}`, { requireSecret: true })

// POST /api/strategy/walk_forward -> rolling walk-forward + Monte Carlo on unseen-data trades.
export const runWalkForwardApi = (strategy, symbol, days, trainDays = 60, testDays = 20) =>
  apiPost(
    `/api/strategy/walk_forward?days=${days}&symbol=${encodeURIComponent(symbol)}&strategy=${encodeURIComponent(strategy)}&train_days=${trainDays}&test_days=${testDays}`,
    undefined,
    { requireSecret: true },
  )
