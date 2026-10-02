import { apiGet } from './client'

// GET /api/strategy/trades -> executed trade journal (SQLite)
export const getTrades = () => apiGet('/api/strategy/trades')
