import { apiGet, apiPost } from './client'

// GET /api/status -> { authenticated, status, user?, message? }
export const getStatus = () => apiGet('/api/status')

// POST /api/login-url { api_key? } -> { login_url }
export const getLoginUrl = (apiKey) => apiPost('/api/login-url', { api_key: apiKey || null })

// POST /api/login { api_key, api_secret, request_token } -> { success, user }
export const login = (apiKey, apiSecret, requestToken) =>
  apiPost('/api/login', {
    api_key: apiKey,
    api_secret: apiSecret,
    request_token: requestToken,
  })

// POST /api/logout (requires shared secret) -> { success, message }
export const logout = () => apiPost('/api/logout', undefined, { requireSecret: true })

// GET /api/profile -> user profile fields
export const getProfile = () => apiGet('/api/profile')

// GET /api/margins -> raw Kite margins object
export const getMargins = () => apiGet('/api/margins')
