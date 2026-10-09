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

let sharedSecret = typeof window !== 'undefined' ? sessionStorage.getItem('app_shared_secret') : null

export function setSharedSecret(value) {
  sharedSecret = value || null
  if (typeof window !== 'undefined') {
    if (value) {
      sessionStorage.setItem('app_shared_secret', value)
    } else {
      sessionStorage.removeItem('app_shared_secret')
    }
  }
}

export function getSharedSecret() {
  return sharedSecret
}

export function hasSharedSecret() {
  return Boolean(sharedSecret)
}

async function request(path, { method = 'GET', body, requireSecret = false, signal, timeoutMs = 10000 } = {}) {
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
  const controller = new AbortController()
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs)
  if (signal) {
    signal.addEventListener('abort', () => controller.abort())
  }

  try {
    res = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    })
  } catch (networkErr) {
    if (networkErr.name === 'AbortError') {
      throw new ApiError('Request timed out after 10s. Backend may be busy.', 0, networkErr.message)
    }
    throw new ApiError('Cannot reach the backend. Is it running?', 0, networkErr.message)
  } finally {
    clearTimeout(timeoutId)
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
