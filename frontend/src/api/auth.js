import { apiGet, apiPost } from './client'

// GET /kite/status -> { connected, user_id, user_name, products, exchanges, message? }
export const getKiteStatus = () => apiGet('/kite/status')

// GET /kite/login -> { login_url }
export const getKiteLoginUrl = () => apiGet('/kite/login')

// POST /kite/logout -> { success, message }
export const kiteLogout = () =>
  apiPost(
    '/kite/logout',
    undefined,
    { requireSecret: true },
  )

// Compatibility exports
export const getStatus = getKiteStatus
export const getLoginUrl = getKiteLoginUrl
export const logout = kiteLogout


