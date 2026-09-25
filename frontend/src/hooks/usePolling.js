import { useCallback, useEffect, useRef, useState } from 'react'

// Fetches `fetcher()` on mount, then every `intervalMs`. Exposes a plain
// state machine (idle/loading/success/error) so pages can render the
// correct empty/error/loading UI instead of guessing from partial data.
// Pass intervalMs = 0 to disable automatic polling (fetch once + manual refresh only).
export function usePolling(fetcher, { intervalMs = 10000, deps = [] } = {}) {
  const [data, setData] = useState(null)
  const [status, setStatus] = useState('loading') // loading | success | error
  const [error, setError] = useState(null)
  const [updatedAt, setUpdatedAt] = useState(null)
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const run = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setStatus((s) => (s === 'success' ? 'success' : 'loading'))
    try {
      const result = await fetcherRef.current()
      setData(result)
      setStatus('success')
      setError(null)
      setUpdatedAt(new Date())
    } catch (err) {
      setError(err)
      setStatus((prev) => (prev === 'success' ? 'success' : 'error'))
    }
  }, [])

  useEffect(() => {
    run()
    if (!intervalMs) return undefined
    const id = setInterval(() => run({ silent: true }), intervalMs)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, status, error, updatedAt, refresh: () => run() }
}
