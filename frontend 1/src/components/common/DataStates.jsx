import { AlertTriangle, Loader2, Inbox, RefreshCw } from 'lucide-react'

export function Loading({ label = 'Loading…' }) {
  return (
    <div className="flex items-center gap-2 py-8 justify-center text-[var(--text-dim)] text-sm">
      <Loader2 className="w-4 h-4 animate-spin" />
      {label}
    </div>
  )
}

export function ErrorState({ error, onRetry, label = 'Unable to load data.' }) {
  const detail = error?.message || 'Unknown error.'
  return (
    <div className="flex flex-col items-center gap-3 py-8 text-center">
      <AlertTriangle className="w-6 h-6 text-[var(--negative)]" />
      <div>
        <p className="text-sm font-medium text-[var(--text)]">{label}</p>
        <p className="text-xs text-[var(--text-dim)] mt-1 font-num">{detail}</p>
      </div>
      {onRetry && (
        <button
          onClick={onRetry}
          className="inline-flex items-center gap-1.5 rounded-md border border-[var(--border-strong)] px-3 py-1.5 text-xs font-medium text-[var(--text)] hover:bg-white/[0.05] transition-colors"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          Retry
        </button>
      )}
    </div>
  )
}

export function EmptyState({ label = 'No data available.', hint }) {
  return (
    <div className="flex flex-col items-center gap-2 py-8 text-center text-[var(--text-dim)]">
      <Inbox className="w-6 h-6 text-[var(--text-faint)]" />
      <p className="text-sm">{label}</p>
      {hint && <p className="text-xs text-[var(--text-faint)]">{hint}</p>}
    </div>
  )
}
