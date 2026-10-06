import { useCallback, useEffect, useRef, useState } from 'react'

// Fetches `fetcher()` on mount, then every `intervalMs`.
// `updatedAt` represents the source timestamp when one is available.
// `fetchedAt` represents the browser/network receipt time.
// They must never be treated as the same thing.
export function usePolling(
  fetcher,
  {
    intervalMs = 10000,
    deps = [],
    getSourceTimestamp = defaultSourceTimestamp,
  } = {},
) {
  const [data, setData] = useState(null)
  const [status, setStatus] = useState('loading')
  const [error, setError] = useState(null)
  const [updatedAt, setUpdatedAt] = useState(null)
  const [fetchedAt, setFetchedAt] = useState(null)

  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  // Monotonically increasing request id. A response is only ever applied
  // to state if it's still the most recent request issued — this is
  // what prevents an older, slower-to-resolve request from overwriting
  // state with stale data after a newer request has already resolved.
  const requestIdRef = useRef(0)

  // Guards against a silent (timer-triggered) poll starting a second,
  // overlapping request while a previous one is still in flight. A
  // user-triggered refresh() always proceeds regardless (see run()
  // below) and supersedes whatever is in flight via requestIdRef.
  const inFlightRef = useRef(false)

  const run = useCallback(async ({ silent = false } = {}) => {
    if (silent && inFlightRef.current) {
      return
    }

    const requestId = ++requestIdRef.current
    inFlightRef.current = true

    if (!silent) {
      setStatus((s) => (s === 'success' ? 'success' : 'loading'))
    }

    try {
      const result = await fetcherRef.current()

      // A newer request has started since this one began (e.g. a
      // manual refresh() superseded a silent poll, or another poll
      // tick fired). This response is stale — applying it now would
      // overwrite data a newer, possibly-already-resolved request
      // already set (or will set). Discard it.
      if (requestId !== requestIdRef.current) {
        return
      }

      const receivedAt = new Date()

      setData(result)
      setStatus('success')
      setError(null)
      setFetchedAt(receivedAt)

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

      setStatus((prev) =>
        prev === 'success'
          ? 'success'
          : 'error',
      )
    } finally {
      // Only the still-current request is allowed to clear the
      // in-flight flag — a superseded request's finally block must not
      // clear it out from under the request that superseded it.
      if (requestId === requestIdRef.current) {
        inFlightRef.current = false
      }
    }
  }, [getSourceTimestamp])

  const refresh = useCallback(
    () => run(),
    [run],
  )

  useEffect(() => {
    run()

    if (!intervalMs) {
      return undefined
    }

    const id = setInterval(
      () => run({ silent: true }),
      intervalMs,
    )

    return () => clearInterval(id)

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return {
    data,
    status,
    error,
    updatedAt,
    fetchedAt,
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