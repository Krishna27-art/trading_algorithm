import { apiGet, apiPost } from './client'

// POST /api/strategy/backtest?days=&symbol=&strategy= -> single-strategy report.
// Supports strategy = gap_fade | rsi_ema | orb_sip.
export const runBacktest = (strategy, symbol, days) =>
  apiPost(
    `/api/strategy/backtest?days=${days}&symbol=${encodeURIComponent(symbol)}&strategy=${encodeURIComponent(strategy)}`,
    undefined,
    { requireSecret: true },
  )

// GET /api/research/backtest?days=&symbol= -> Gap Fade vs RSI x EMA vs ORB-SIP comparison
export const getResearchBacktest = (days, symbol) =>
  apiGet(`/api/research/backtest?days=${days}&symbol=${encodeURIComponent(symbol)}`, { requireSecret: true })

// POST /api/strategy/walk_forward -> rolling walk-forward + Monte Carlo on unseen-data trades.
export const runWalkForwardApi = (strategy, symbol, days, trainDays = 60, testDays = 20) =>
  apiPost(
    `/api/strategy/walk_forward?days=${days}&symbol=${encodeURIComponent(symbol)}&strategy=${encodeURIComponent(strategy)}&train_days=${trainDays}&test_days=${testDays}`,
    undefined,
    { requireSecret: true },
  )
