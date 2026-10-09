import { useCallback, useEffect, useRef, useState } from 'react'

// Fetches `fetcher()` on mount, then every `intervalMs`.
// `updatedAt` represents the source timestamp when one is available.
// `fetchedAt` represents the browser/network receipt time.
// They must never be treated as the same thing.
export function usePolling(
  fetcher,
  {
    intervalMs = 10000,
    staleThresholdMs = 30000,
    deps = [],
    getSourceTimestamp = defaultSourceTimestamp,
  } = {},
) {
  const [data, setData] = useState(null)
  const [status, setStatus] = useState('loading')
  const [error, setError] = useState(null)
  const [updatedAt, setUpdatedAt] = useState(null)
  const [fetchedAt, setFetchedAt] = useState(null)
  const [lastSuccessAt, setLastSuccessAt] = useState(null)
  const [now, setNow] = useState(() => Date.now())

  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const requestIdRef = useRef(0)
  const inFlightRef = useRef(false)

  const run = useCallback(async ({ silent = false } = {}) => {
    if (silent && inFlightRef.current) {
      return
    }

    const requestId = ++requestIdRef.current
    inFlightRef.current = true

    if (!silent && !data) {
      setStatus('loading')
    }

    try {
      const result = await fetcherRef.current()

      if (requestId !== requestIdRef.current) {
        return
      }

      const receivedAt = new Date()

      setData(result)
      setStatus('success')
      setError(null)
      setFetchedAt(receivedAt)
      setLastSuccessAt(receivedAt)

      const sourceTimestamp = getSourceTimestamp(result)

      if (sourceTimestamp) {
        const parsed = new Date(sourceTimestamp)

        setUpdatedAt(
          Number.isNaN(parsed.getTime())
            ? null
            : parsed,
        )
      } else {
        setUpdatedAt(null)
      }
    } catch (err) {
      if (requestId !== requestIdRef.current) {
        return
      }

      setError(err)
      setStatus(data ? 'stale' : 'error')
    } finally {
      if (requestId === requestIdRef.current) {
        inFlightRef.current = false
      }
    }
  }, [data, getSourceTimestamp])

  const refresh = useCallback(
    () => run(),
    [run],
  )

  useEffect(() => {
    run()

    if (!intervalMs) {
      return undefined
    }

    const id = setInterval(() => {
      setNow(Date.now())
      run({ silent: true })
    }, intervalMs)

    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  const sourceTime = updatedAt ? updatedAt.getTime() : (lastSuccessAt ? lastSuccessAt.getTime() : null)
  const dataAgeSeconds = sourceTime ? Math.max(0, Math.round((now - sourceTime) / 1000)) : null
  const isStale = Boolean(
    error ||
    status === 'stale' ||
    (sourceTime && (now - sourceTime) > (staleThresholdMs || intervalMs * 2.5))
  )

  return {
    data,
    status: isStale && data ? 'stale' : status,
    error,
    updatedAt,
    fetchedAt,
    lastSuccessAt,
    dataAgeSeconds,
    isStale,
    refresh,
  }
}

function defaultSourceTimestamp(result) {
  if (!result || typeof result !== 'object') {
    return null
  }

  if (result.data_timestamp) {
    return result.data_timestamp
  }

  if (result.last_tick_time) {
    return result.last_tick_time
  }

  if (result.timestamp) {
    return result.timestamp
  }

  return null
}