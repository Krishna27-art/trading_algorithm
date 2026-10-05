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

  const run = useCallback(async ({ silent = false } = {}) => {
    if (!silent) {
      setStatus((s) => (s === 'success' ? 'success' : 'loading'))
    }

    try {
      const result = await fetcherRef.current()

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
