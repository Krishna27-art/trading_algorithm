import { useMemo, useState } from 'react'
import { ChevronDown, ChevronUp, ArrowUpRight, ArrowDownRight } from 'lucide-react'
import Card from '../components/common/Card'
import StatusPill from '../components/common/StatusPill'
import Timestamp from '../components/common/Timestamp'
import { Loading, ErrorState, EmptyState } from '../components/common/DataStates'
import { usePolling } from '../hooks/usePolling'
import { getLiveResearch } from '../api/market'
import { formatCurrency } from '../utils/format'

const STRATEGIES = [
  { id: 'all', label: 'All' },
  { id: 'orb', label: 'ORB' },
  { id: 'cpr', label: 'CPR' },
  { id: 'dual_ema', label: 'Dual EMA' },
  { id: 'nse_rm_100', label: 'NSE-RM-100' },
  { id: 'nse_vrp_index', label: 'NSE-VRP-INDEX' },
  { id: 'apex', label: 'APEX-AIVEM' },
]

const ALL_STRATEGY_KEYS = ['orb', 'cpr', 'dual_ema', 'nse_rm_100', 'nse_vrp_index', 'apex']

export default function LiveSignalsPage() {
  const [filter, setFilter] = useState('all')
  const [expanded, setExpanded] = useState(() => new Set())
  // Backend caches this endpoint for 15s server-side; polling faster wouldn't
  // return fresher data, so we match that cadence.
  const research = usePolling(() => getLiveResearch(10, false), { intervalMs: 15000 })

  const candidates = research.data?.candidates || []
  const visible = useMemo(() => {
    if (filter === 'all') return candidates
    return candidates.filter((c) => {
      const preds = c.strategies || c.predictions || {}
      return preds[filter] && preds[filter].status !== 'UNAVAILABLE'
    })
  }, [candidates, filter])

  const toggle = (symbol) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      next.has(symbol) ? next.delete(symbol) : next.add(symbol)
      return next
    })
  }

  return (
    <div className="space-y-4 animate-fade-in">
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-1 rounded-md border border-[var(--border-strong)] p-1 flex-wrap">
            {STRATEGIES.map((s) => (
              <button
                key={s.id}
                onClick={() => setFilter(s.id)}
                className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
                  filter === s.id ? 'bg-[var(--accent-dim)] text-[var(--accent)]' : 'text-[var(--text-dim)] hover:text-[var(--text)]'
                }`}
              >
                {s.label}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-3">
            {research.data && (
              <StatusPill tone={research.data.data_source === 'REAL_KITE' ? 'positive' : 'warning'}>
                {research.data.data_source === 'REAL_KITE' ? 'Live Kite data' : research.data.data_source}
              </StatusPill>
            )}
            <Timestamp updatedAt={research.updatedAt} staleAfterMs={20000} />
          </div>
        </div>
      </Card>

      {research.status === 'loading' && !research.data ? (
        <Card><Loading label="Scanning 300-stock universe…" /></Card>
      ) : research.status === 'error' && !research.data ? (
        <Card><ErrorState error={research.error} onRetry={research.refresh} /></Card>
      ) : research.data?.status === 'AUTH_REQUIRED' ? (
        <Card>
          <EmptyState label="Live signals require a connected Kite session." hint={research.data.message} />
        </Card>
      ) : (
        <>
          <KeyInsights insights={research.data?.key_insights} />

          {visible.length === 0 ? (
            <Card>
              <EmptyState label={filter === 'all' ? 'No candidates scanned yet.' : `No active ${labelFor(filter)} signals right now.`} />
            </Card>
          ) : (
            <div className="space-y-3">
              {visible.map((c) => (
                <SignalRow
                  key={c.symbol}
                  candidate={c}
                  filter={filter}
                  expanded={expanded.has(c.symbol)}
                  onToggle={() => toggle(c.symbol)}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}

function KeyInsights({ insights }) {
  if (!insights) return null
  const items = [
    { key: 'top_long', label: 'Top long', tone: 'positive' },
    { key: 'top_short', label: 'Top short', tone: 'negative' },
    { key: 'strongest_consensus', label: 'Strongest consensus', tone: 'accent' },
  ].filter((i) => insights[i.key])

  if (items.length === 0 && (!insights.divergent_signals || insights.divergent_signals.length === 0)) return null

  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
      {items.map(({ key, label, tone }) => {
        const c = insights[key]
        return (
          <Card key={key}>
            <p className="text-xs text-[var(--text-faint)] mb-1">{label}</p>
            <div className="flex items-center justify-between">
              <span className="font-semibold font-num">{c.symbol}</span>
              <StatusPill tone={tone}>{c.consensus?.label}</StatusPill>
            </div>
            <p className="text-xs text-[var(--text-dim)] mt-1 font-num">LTP {formatCurrency(c.ltp)}</p>
          </Card>
        )
      })}
    </div>
  )
}

function SignalRow({ candidate, filter, expanded, onToggle }) {
  const preds = candidate.strategies || candidate.predictions || {}
  const focused = filter !== 'all' ? preds[filter] : null

  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <span className="font-semibold font-num text-base">{candidate.symbol}</span>
          <span className="text-sm font-num text-[var(--text-dim)]">{formatCurrency(candidate.ltp)}</span>
          <StatusPill tone={consensusTone(candidate.consensus?.direction)}>{candidate.consensus?.label}</StatusPill>
        </div>
        <button
          onClick={onToggle}
          className="flex items-center gap-1 text-xs text-[var(--accent)] hover:underline"
        >
          {expanded ? 'Hide details' : 'View details'}
          {expanded ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
        </button>
      </div>

      {filter === 'all' ? (
        <div className="mt-3 flex flex-wrap gap-2">
          {ALL_STRATEGY_KEYS.map((k) => (
            <StrategyChip key={k} name={k} pred={preds[k]} />
          ))}
        </div>
      ) : (
        focused && (
          <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-3">
            <MiniStat label="Status" value={focused.status?.replaceAll('_', ' ')} tone={dirTone(focused.direction, focused.status)} />
            <MiniStat label="Direction" value={focused.direction || '—'} tone={dirTone(focused.direction, focused.status)} />
            <MiniStat label="Entry" value={formatCurrency(focused.entry)} />
            <MiniStat label="Stop loss" value={formatCurrency(focused.stop_loss)} tone="negative" />
          </div>
        )
      )}

      {expanded && (
        <div className="mt-4 pt-4 border-t border-[var(--border)] space-y-3">
          {ALL_STRATEGY_KEYS.map((k) => (
            <StrategyDetail key={k} name={k} pred={preds[k]} />
          ))}
        </div>
      )}
    </Card>
  )
}

function StrategyChip({ name, pred }) {
  if (!pred) return null
  const tone = dirTone(pred.direction, pred.status)
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs font-medium ${chipTone(tone)}`}>
      {pred.direction === 'LONG' && <ArrowUpRight className="w-3 h-3" />}
      {pred.direction === 'SHORT' && <ArrowDownRight className="w-3 h-3" />}
      {labelFor(name)}: {pred.status.replaceAll('_', ' ')}
    </span>
  )
}

function StrategyDetail({ name, pred }) {
  if (!pred) return null
  return (
    <div className="rounded-lg border border-[var(--border)] p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-semibold">{labelFor(name)}</span>
        <StatusPill tone={dirTone(pred.direction, pred.status)} dot={false}>
          {pred.status.replaceAll('_', ' ')}
        </StatusPill>
      </div>
      {pred.direction && (
        <div className="grid grid-cols-3 gap-3 mb-2">
          <MiniStat label="Entry" value={formatCurrency(pred.entry)} />
          <MiniStat label="Stop loss" value={formatCurrency(pred.stop_loss)} tone="negative" />
          <MiniStat label="Target" value={formatCurrency(pred.target)} tone="positive" />
        </div>
      )}
      {pred.reason && <p className="text-xs text-[var(--text-dim)] mb-2">{pred.reason}</p>}
      {pred.levels && Object.keys(pred.levels).length > 0 && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 mb-1">
          {Object.entries(pred.levels).map(([k, v]) => (
            <span key={k} className="text-xs font-num text-[var(--text-faint)]">
              {k}: <span className="text-[var(--text-dim)]">{String(v)}</span>
            </span>
          ))}
        </div>
      )}
      {pred.metrics && Object.keys(pred.metrics).length > 0 && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 mt-1 pt-1 border-t border-[var(--border)]/40">
          {Object.entries(pred.metrics).map(([k, v]) => (
            <span key={k} className="text-xs font-num text-[var(--text-faint)]">
              {k}: <span className="text-[var(--accent)] font-medium">{String(v)}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function MiniStat({ label, value, tone }) {
  const color = tone === 'positive' ? 'text-[var(--positive)]' : tone === 'negative' ? 'text-[var(--negative)]' : tone === 'warning' ? 'text-[var(--warning)]' : 'text-[var(--text)]'
  return (
    <div>
      <p className="text-xs text-[var(--text-faint)]">{label}</p>
      <p className={`text-sm font-num font-medium ${color}`}>{value || '—'}</p>
    </div>
  )
}

function dirTone(direction, status) {
  if (direction === 'LONG') return 'positive'
  if (direction === 'SHORT') return 'negative'
  if (status === 'UNAVAILABLE' || status === 'ERROR') return 'warning'
  return 'neutral'
}
function chipTone(tone) {
  if (tone === 'positive') return 'text-[var(--positive)] bg-[var(--positive-dim)] border-[var(--positive)]/30'
  if (tone === 'negative') return 'text-[var(--negative)] bg-[var(--negative-dim)] border-[var(--negative)]/30'
  if (tone === 'warning') return 'text-[var(--warning)] bg-[var(--warning-dim)] border-[var(--warning)]/30'
  return 'text-[var(--text-dim)] bg-white/[0.04] border-[var(--border-strong)]'
}
function consensusTone(direction) {
  if (direction === 'LONG') return 'positive'
  if (direction === 'SHORT') return 'negative'
  if (direction === 'DIVERGENT') return 'warning'
  return 'neutral'
}
function labelFor(key) {
  return (
    {
      orb: 'ORB',
      cpr: 'CPR',
      dual_ema: 'Dual EMA',
      nse_rm_100: 'NSE-RM-100',
      nse_vrp_index: 'NSE-VRP-INDEX',
      apex: 'APEX-AIVEM',
    }[key] || key
  )
}
