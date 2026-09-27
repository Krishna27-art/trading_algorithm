import { useState } from 'react'
import { X } from 'lucide-react'
import { setSharedSecret, hasSharedSecret } from '../../api/client'

export default function SettingsModal({ onClose }) {
  const [secret, setSecret] = useState('')
  const [saved, setSaved] = useState(hasSharedSecret())

  const save = () => {
    setSharedSecret(secret.trim())
    setSaved(true)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="w-full max-w-md rounded-xl border border-[var(--border-strong)] bg-[var(--panel)] p-5 space-y-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold">Settings</h2>
          <button onClick={onClose} className="text-[var(--text-dim)] hover:text-[var(--text)]">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="space-y-2">
          <label className="text-xs font-medium text-[var(--text-dim)]">System Architecture</label>
          <div className="rounded-md border border-[var(--border)] bg-white/[0.02] p-3 text-xs text-[var(--text-dim)] space-y-1">
            <p className="font-semibold text-[var(--text)]">Read-Only Decision Support</p>
            <p>
              This system streams live Zerodha Kite market data, calculates intraday indicators, scans the universe, and generates strategy signals for manual trade execution in Kite.
            </p>
          </div>
        </div>

        <div className="space-y-2">
          <label className="text-xs font-medium text-[var(--text-dim)]">Backend shared secret</label>
          <input
            type="password"
            value={secret}
            onChange={(e) => {
              setSecret(e.target.value)
              setSaved(false)
            }}
            placeholder="APP_SHARED_SECRET from .env"
            className="w-full rounded-md bg-black/30 border border-[var(--border-strong)] px-3 py-2 text-sm font-num focus:border-[var(--accent)] outline-none"
          />
          <p className="text-xs text-[var(--text-faint)]">
            Required by protected backend management routes. Held in memory for this session only — never saved to disk.
          </p>
          <button
            onClick={save}
            disabled={!secret.trim()}
            className="w-full rounded-md bg-[var(--accent-dim)] border border-[var(--accent)]/30 text-[var(--accent)] py-2 text-sm font-medium disabled:opacity-40 hover:bg-[var(--accent)]/20 transition-colors"
          >
            {saved ? 'Saved for this session' : 'Save for this session'}
          </button>
        </div>
      </div>
    </div>
  )
}

