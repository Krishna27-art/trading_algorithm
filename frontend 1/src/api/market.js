import { apiGet } from './client'

// GET /api/strategy/telemetry?symbol=&strategy= -> live price, active signal,
// active trade, risk summary, chart candles for one symbol/strategy pair.
export const getTelemetry = (symbol, strategy) =>
  apiGet(`/api/strategy/telemetry?symbol=${encodeURIComponent(symbol)}&strategy=${encodeURIComponent(strategy)}`)

// GET /api/research/live?top_n=&force_refresh= -> ranked candidates with
// per-strategy predictions (orb/cpr/dual_ema), consensus, key_insights.
export const getLiveResearch = (topN = 10, forceRefresh = false) =>
  apiGet(`/api/research/live?top_n=${topN}${forceRefresh ? '&force_refresh=true' : ''}`)

// GET /api/strategy/state?strategy= -> strategy metadata, schedule, risk config
export const getStrategyState = (strategy) =>
  apiGet(`/api/strategy/state${strategy ? `?strategy=${encodeURIComponent(strategy)}` : ''}`)

// GET /api/strategy/scanner?top_n=&refresh= -> ranked NIFTY-50 universe scan
export const getScanner = (topN = 5, refresh = false) =>
  apiGet(`/api/strategy/scanner?top_n=${topN}${refresh ? '&refresh=true' : ''}`)
