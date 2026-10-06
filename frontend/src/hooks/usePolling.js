import { useCallback, useEffect, useRef, useState } from 'react'

// Fetches `fetcher()` on mount, then every `intervalMs`.
// `updatedAt` represents the source timestamp when one is available.
// `fetchedAt` represents the browser/network receipt time.
// They must never be treated as the same thing.
//
// Overlapping-request protection: each fetch increments a generation
// counter before sending the request. Only the response whose generation
// matches the current counter is accepted. An older in-flight response
// that arrives after a newer one is silently discarded.
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

  // Monotonically increasing counter. Each request captures its value
  // before awaiting so stale responses can be detected.
  const generationRef = useRef(0)

  const run = useCallback(async ({ silent = false } = {}) => {
    generationRef.current += 1
    const myGeneration = generationRef.current

    if (!silent) {
      setStatus((s) => (s === 'success' ? 'success' : 'loading'))
    }

    try {
      const result = await fetcherRef.current()

      // Discard this response if a newer request has already completed.
      if (myGeneration !== generationRef.current) {
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
      // Discard error from a stale request.
      if (myGeneration !== generationRef.current) {
        return
      }

      setError(err)

      setStatus((prev) =>
        prev === 'success'
          ? 'success'
          : 'error',
      )
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
