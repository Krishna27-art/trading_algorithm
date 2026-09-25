import { useState } from 'react'
import { X } from 'lucide-react'
import { setSharedSecret, hasSharedSecret } from '../../api/client'

export default function SettingsModal({ onClose, tradingMode, onChangeTradingMode }) {
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
          <label className="text-xs font-medium text-[var(--text-dim)]">Trading mode</label>
          <div className="flex rounded-md border border-[var(--border-strong)] overflow-hidden text-sm">
            {['PAPER', 'LIVE'].map((mode) => (
              <button
                key={mode}
                onClick={() => onChangeTradingMode(mode)}
                className={`flex-1 py-1.5 font-medium transition-colors ${
                  tradingMode === mode
                    ? mode === 'LIVE'
                      ? 'bg-[var(--negative-dim)] text-[var(--negative)]'
                      : 'bg-[var(--accent-dim)] text-[var(--accent)]'
                    : 'text-[var(--text-dim)] hover:bg-white/[0.05]'
                }`}
              >
                {mode === 'LIVE' ? 'Live money' : 'Paper simulation'}
              </button>
            ))}
          </div>
          <p className="text-xs text-[var(--text-faint)]">
            Controls whether order actions route to your live Zerodha account or the backend's paper simulator.
          </p>
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
            Required by the backend to place orders, exit positions, or log out. Held in memory for this
            session only — never saved to disk.
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
