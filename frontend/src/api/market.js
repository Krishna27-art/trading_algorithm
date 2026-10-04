import { apiGet } from './client'

// GET /api/strategy/state?strategy= -> strategy metadata, schedule, risk config
export const getStrategyState = (strategy) =>
  apiGet(`/api/strategy/state${strategy ? `?strategy=${encodeURIComponent(strategy)}` : ''}`)

// GET /api/market/prices -> 700-stock universe live market quotes
export const getMarketPrices = () => apiGet('/api/market/prices')

// GET /api/risk/summary -> portfolio risk usage & capital metrics
export const getRiskSummary = () => apiGet('/api/risk/summary')
