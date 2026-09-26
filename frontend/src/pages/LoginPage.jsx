import { useState, useEffect } from 'react'
import { ExternalLink, AlertCircle, Loader2 } from 'lucide-react'
import { getKiteLoginUrl } from '../api/auth'
import { ApiError } from '../api/client'

export default function LoginPage({ onContinuePaper, authError }) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(authError || '')

  useEffect(() => {
    if (authError) {
      setError(authError)
    }
  }, [authError])

  const handleConnectKite = async () => {
    setError('')
    setLoading(true)
    try {
      const data = await getKiteLoginUrl()
      if (data && data.login_url) {
        window.location.href = data.login_url
      } else {
        setError('Could not retrieve login URL from backend.')
        setLoading(false)
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not reach the backend server.')
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-4 bg-[var(--bg)] text-[var(--text)]">
      <div className="w-full max-w-md rounded-xl border border-[var(--border-strong)] bg-[var(--panel)] p-6 space-y-6">
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className="w-2.5 h-2.5 rounded-full bg-amber-500 animate-pulse inline-block"></span>
            <span className="text-xs font-semibold uppercase tracking-wider text-[var(--text-dim)]">
              ○ Kite Not Connected
            </span>
          </div>
          <h1 className="text-xl font-bold tracking-tight">Zerodha Kite Connect</h1>
          <p className="text-sm text-[var(--text-dim)] leading-relaxed">
            Click below to initiate official Zerodha OAuth login. Your request token and session token are processed automatically via the backend callback.
          </p>
        </div>

        {error && (
          <div className="flex items-start gap-2 rounded-md border border-[var(--negative)]/30 bg-[var(--negative-dim)] px-3 py-2 text-sm text-[var(--negative)]">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <span>{error}</span>
          </div>
        )}

        <div className="space-y-3 pt-2">
          <button
            type="button"
            onClick={handleConnectKite}
            disabled={loading}
            className="w-full flex items-center justify-center gap-2 rounded-lg bg-[var(--accent-dim)] border border-[var(--accent)]/30 text-[var(--accent)] py-3 text-sm font-semibold hover:bg-[var(--accent)]/20 transition-all shadow-sm disabled:opacity-50 cursor-pointer"
          >
            {loading ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin text-[var(--accent)]" />
                <span>Redirecting to Kite…</span>
              </>
            ) : (
              <>
                <ExternalLink className="w-4 h-4" />
                <span>Connect Kite</span>
              </>
            )}
          </button>
        </div>

        <div className="pt-4 border-t border-[var(--border)] text-center">
          <button
            onClick={onContinuePaper}
            className="text-xs text-[var(--text-dim)] hover:text-[var(--text)] underline underline-offset-2 cursor-pointer"
          >
            Continue without connecting (paper trading, no live market data)
          </button>
        </div>
      </div>
    </div>
  )
}
