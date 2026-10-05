import { apiGet } from './client'

// GET /api/strategy/state?strategy= -> strategy metadata, schedule, risk config
export const getStrategyState = (strategy) =>
  apiGet(`/api/strategy/state${strategy ? `?strategy=${encodeURIComponent(strategy)}` : ''}`)

// GET /api/stream/market -> canonical streaming market state for the 700-stock universe
export const getMarketPrices = async () => {
  const data = await apiGet('/api/stream/market')
  const instruments = data?.instruments || {}
  const stocks = Object.values(instruments)
  return {
    ...data,
    stocks,
    data_source: data?.data_fresh ? 'STREAM' : 'STREAM_STALE',
  }
}

