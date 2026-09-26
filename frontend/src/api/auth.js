import { apiGet, apiPost } from './client'

// GET /kite/status -> { connected, user_id, user_name, products, exchanges, message? }
export const getKiteStatus = () => apiGet('/kite/status')

// GET /kite/login -> { login_url }
export const getKiteLoginUrl = () => apiGet('/kite/login')

// POST /kite/logout -> { success, message }
export const kiteLogout = () => apiPost('/kite/logout')

// Compatibility exports
export const getStatus = getKiteStatus
export const getLoginUrl = getKiteLoginUrl
export const logout = kiteLogout

// GET /api/profile -> user profile fields
export const getProfile = () => apiGet('/api/profile')

// GET /api/margins -> raw Kite margins object
export const getMargins = () => apiGet('/api/margins')
