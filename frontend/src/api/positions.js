import { apiGet, apiPost } from './client'

// GET /api/portfolio/positions -> normalized broker positions + totals
export const getPositions = () => apiGet('/api/portfolio/positions')

// GET /api/strategy/trades -> executed trade journal (SQLite)
export const getTrades = () => apiGet('/api/strategy/trades')

// GET /api/strategy/orders -> last 50 order records
export const getOrders = () => apiGet('/api/strategy/orders')

// POST /api/orders/place (requires shared secret)
export const placeOrder = ({ symbol, direction, orderType, price, quantity, mode }) =>
  apiPost(
    '/api/orders/place',
    {
      symbol,
      direction,
      order_type: orderType || 'LIMIT',
      price: price ?? null,
      quantity,
      mode: mode || 'PAPER',
    },
    { requireSecret: true },
  )

// POST /api/orders/exit (requires shared secret)
export const exitOrder = (symbol, mode) =>
  apiPost('/api/orders/exit', { symbol, mode: mode || 'PAPER' }, { requireSecret: true })
