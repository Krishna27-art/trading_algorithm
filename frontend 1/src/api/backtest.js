import { apiGet, apiPost } from './client'

// POST /api/strategy/backtest?days=&symbol=&strategy= -> single-strategy report.
// Supports strategy = orb | cpr | dual_ema | rm100 (backend has no vrp backtest route).
export const runBacktest = (strategy, symbol, days) =>
  apiPost(
    `/api/strategy/backtest?days=${days}&symbol=${encodeURIComponent(symbol)}&strategy=${encodeURIComponent(strategy)}`,
  )

// GET /api/research/backtest?days=&symbol= -> ORB vs CPR vs Dual-EMA comparison
export const getResearchBacktest = (days, symbol) =>
  apiGet(`/api/research/backtest?days=${days}&symbol=${encodeURIComponent(symbol)}`)
