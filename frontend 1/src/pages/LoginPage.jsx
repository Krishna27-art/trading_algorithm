import { useState } from 'react'
import { ExternalLink, AlertCircle, Loader2, ArrowRight } from 'lucide-react'
import { getLoginUrl, login } from '../api/auth'
import { ApiError } from '../api/client'

export default function LoginPage({ onLoginSuccess, onContinuePaper }) {
  const [apiKey, setApiKey] = useState('')
  const [apiSecret, setApiSecret] = useState('')
  const [requestToken, setRequestToken] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const handleOpenLogin = async () => {
    setError('')
    if (!apiKey.trim()) {
      setError('Enter your Kite API key first.')
      return
    }
    try {
      const data = await getLoginUrl(apiKey.trim())
      window.open(data.login_url, '_blank', 'noopener,noreferrer')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not reach the backend.')
    }
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    if (!apiKey.trim() || !apiSecret.trim() || !requestToken.trim()) {
      setError('API key, API secret, and request token are all required.')
      return
    }
    setLoading(true)
    try {
      const data = await login(apiKey.trim(), apiSecret.trim(), requestToken.trim())
      onLoginSuccess(data.user)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Login failed.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-4 bg-[var(--bg)] text-[var(--text)]">
      <div className="w-full max-w-md rounded-xl border border-[var(--border-strong)] bg-[var(--panel)] p-6 space-y-5">
        <div>
          <h1 className="text-lg font-semibold">Connect your Zerodha account</h1>
          <p className="text-sm text-[var(--text-dim)] mt-1">
            Live signals, positions, and orders all come from your Kite session. Nothing on this screen is
            stored anywhere except the backend's local session file.
          </p>
        </div>

        {error && (
          <div className="flex items-start gap-2 rounded-md border border-[var(--negative)]/30 bg-[var(--negative-dim)] px-3 py-2 text-sm text-[var(--negative)]">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <span>{error}</span>
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-3">
          <Field label="API key" value={apiKey} onChange={setApiKey} />
          <button
            type="button"
            onClick={handleOpenLogin}
            className="w-full flex items-center justify-center gap-2 rounded-md border border-[var(--border-strong)] py-2 text-sm font-medium hover:bg-white/[0.05] transition-colors"
          >
            <ExternalLink className="w-4 h-4" />
            Open Zerodha login
          </button>

          <Field label="API secret" value={apiSecret} onChange={setApiSecret} type="password" />
          <Field
            label="Request token"
            value={requestToken}
            onChange={setRequestToken}
            hint="Paste the request_token from the redirect URL after logging in."
          />

          <button
            type="submit"
            disabled={loading}
            className="w-full flex items-center justify-center gap-2 rounded-md bg-[var(--accent-dim)] border border-[var(--accent)]/30 text-[var(--accent)] py-2.5 text-sm font-medium hover:bg-[var(--accent)]/20 transition-colors disabled:opacity-50"
          >
            {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <ArrowRight className="w-4 h-4" />}
            {loading ? 'Authenticating…' : 'Connect'}
          </button>
        </form>

        <div className="pt-3 border-t border-[var(--border)] text-center">
          <button
            onClick={onContinuePaper}
            className="text-xs text-[var(--text-dim)] hover:text-[var(--text)] underline underline-offset-2"
          >
            Continue without connecting (paper trading, no live market data)
          </button>
        </div>
      </div>
    </div>
  )
}

function Field({ label, value, onChange, type = 'text', hint }) {
  return (
    <div className="space-y-1">
      <label className="text-xs font-medium text-[var(--text-dim)]">{label}</label>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-md bg-black/30 border border-[var(--border-strong)] px-3 py-2 text-sm font-num focus:border-[var(--accent)] outline-none"
      />
      {hint && <p className="text-xs text-[var(--text-faint)]">{hint}</p>}
    </div>
  )
}
