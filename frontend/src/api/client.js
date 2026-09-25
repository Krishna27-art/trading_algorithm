// Central API client. Every request to the backend goes through here.
// This layer never invents data: on failure it throws, and callers are
// responsible for showing a loading / error / empty state.

export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

// Shared secret required by the backend for sensitive actions
// (logout, placing orders, exiting orders). Held in memory only.
let sharedSecret = null

export function setSharedSecret(value) {
  sharedSecret = value || null
}

export function getSharedSecret() {
  return sharedSecret
}

export function hasSharedSecret() {
  return Boolean(sharedSecret)
}

async function request(path, { method = 'GET', body, requireSecret = false, signal } = {}) {
  const headers = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (requireSecret) {
    if (!sharedSecret) {
      throw new ApiError(
        'This action requires the backend shared secret. Add it in Settings before continuing.',
        401,
        null,
      )
    }
    headers['X-Shared-Secret'] = sharedSecret
  }

  let res
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
    })
  } catch (networkErr) {
    throw new ApiError('Cannot reach the backend. Is it running?', 0, networkErr.message)
  }

  let payload = null
  const text = await res.text()
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = null
    }
  }

  if (!res.ok) {
    const detail = payload?.detail || payload?.message
    throw new ApiError(
      typeof detail === 'string' ? detail : `Backend returned HTTP ${res.status}`,
      res.status,
      detail,
    )
  }

  return payload
}

export const apiGet = (path, opts) => request(path, { ...opts, method: 'GET' })
export const apiPost = (path, body, opts) => request(path, { ...opts, method: 'POST', body })
