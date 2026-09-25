import { apiGet } from './client'

// GET /api/system/health -> CONNECTED/DISCONNECTED status of subsystems
export const getSystemHealth = () => apiGet('/api/system/health')
