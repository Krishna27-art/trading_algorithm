import { apiGet } from './client'

// GET /api/portfolio/positions -> normalized broker positions + totals
export const getPositions = () => apiGet('/api/portfolio/positions')

// GET /api/strategy/trades -> executed trade journal (SQLite)
export const getTrades = () => apiGet('/api/strategy/trades')

// GET /api/strategy/orders -> last 50 order records
export const getOrders = () => apiGet('/api/strategy/orders')
