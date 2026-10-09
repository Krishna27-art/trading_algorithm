import { useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, ChevronUp, ArrowUpRight, ArrowDownRight, Play, Square } from 'lucide-react'
import Card from '../components/common/Card'
import StatusPill from '../components/common/StatusPill'
import Timestamp from '../components/common/Timestamp'
import { Loading, ErrorState, EmptyState } from '../components/common/DataStates'
import StrategyPerformanceSection from '../components/StrategyPerformanceSection'
import { usePolling } from '../hooks/usePolling'
import { apiGet, apiPost, hasSharedSecret } from '../api/client'

const STRATEGIES = [
  { id: 'all', label: 'All' },
  { id: 'orb', label: 'ORB' },
  { id: 'cpr', label: 'CPR' },
  { id: 'dual_ema', label: 'Dual EMA' },
  { id: 'apex', label: 'APEX-AIVEM' },
  { id: 'sector_impulse', label: 'Sector Impulse' },
  { id: 'ssf_l5_srm', label: 'SSF-L5-SRM' },
  { id: 'aou_oss', label: 'AOU-OSS' },
  { id: 'crsd', label: 'CRSD' },
]

const ALL_STRATEGY_KEYS = [
  'orb',
  'cpr',
  'dual_ema',
  'apex',
  'sector_impulse',
  'ssf_l5_srm',
  'aou_oss',
  'crsd',
]

const MAX_AUTO_START_ATTEMPTS = 5
const AUTO_START_RETRY_MS = 15000

const SHARED_SECRET_MISSING_MESSAGE =
  'Backend shared secret is not configured. Add it in Settings before starting the live market stream.'

/**
 * The live-signal page intentionally uses the WebSocket-backed endpoints:
 *
 *   KiteTicker
 *      -> MarketStreamManager
 *      -> CandleAggregator
 *      -> LiveMarketState / LiveSignalEngine
 *      -> GET /api/stream/signals
 *
 * It does NOT use /api/research/live because that is a separate historical/
 * REST research path and is not the authoritative streaming signal source.
 *
 * This page contains no broker order execution controls.
 */
export default function LiveSignalsPage({ isAuthenticated }) {
  const [filter, setFilter] = useState('all')
  const [expanded, setExpanded] = useState(() => new Set())
  const [streamAction, setStreamAction] = useState('idle')
  const [streamError, setStreamError] = useState(null)
  const streamStartInFlight = useRef(false)
  const autoStartAttempts = useRef(0)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const status = usePolling(() => apiGet('/api/stream/status'), {
    intervalMs: 5000,
  })

  const signals = usePolling(() => apiGet('/api/stream/signals'), {
    intervalMs: 5000,
  })

  const market = usePolling(() => apiGet('/api/stream/market'), {
    intervalMs: 3000,
  })

  const signalFeedFresh =
    signals.data?.data_fresh === true &&
    signals.data?.stream_connected === true

  const marketFeedFresh =
    market.data?.data_fresh === true &&
    market.data?.stream_connected === true

  /**
   * Start the read-only market-data stream automatically after the user has
   * authenticated. No order/trade action occurs here.
   *
   * Guards:
   *  - backend stream state, so renders never open duplicate connections
   *  - an in-flight ref, so overlapping start requests are impossible
   *  - a bounded retry (see the retry effect below) so a failed start can
   *    recover on its own without hammering the backend in a tight loop
   */
  useEffect(() => {
    if (!isAuthenticated) return
    if (status.status !== 'success') return

    if (status.data?.connected) {
      autoStartAttempts.current = 0
      return
    }

    const state = status.data?.state
    if (state === 'CONNECTING' || state === 'RECONNECTING') return

    // 'error' is only cleared by the retry effect (after a delay) or by the
    // user. Without this guard the effect re-fires immediately after a failure.
    if (streamAction === 'starting' || streamAction === 'error') return
    if (streamStartInFlight.current) return

    // A missing secret is surfaced in the UI (see secretError below).
    if (!hasSharedSecret()) return
    if (autoStartAttempts.current >= MAX_AUTO_START_ATTEMPTS) return

    const start = async () => {
      streamStartInFlight.current = true
      autoStartAttempts.current += 1
      setStreamAction('starting')

      try {
        await apiPost('/api/stream/start', {}, { requireSecret: true })
        if (!mounted.current) return

        setStreamError(null)
        setStreamAction('started')
        status.refresh()
      } catch (error) {
        if (!mounted.current) return

        setStreamAction('error')
        setStreamError(error)
      } finally {
        streamStartInFlight.current = false
      }
    }

    start()
  }, [
    isAuthenticated,
    status.status,
    status.data?.connected,
    status.data?.state,
    status.refresh,
    streamAction,
  ])

  /**
   * Bounded automatic retry: after a failed start, wait, then return to 'idle'
   * so the startup effect above may try again (up to MAX_AUTO_START_ATTEMPTS).
   */
  useEffect(() => {
    if (streamAction !== 'error') return
    if (!isAuthenticated || !hasSharedSecret()) return
    if (autoStartAttempts.current >= MAX_AUTO_START_ATTEMPTS) return

    const timer = setTimeout(() => {
      if (mounted.current) setStreamAction('idle')
    }, AUTO_START_RETRY_MS)

    return () => clearTimeout(timer)
  }, [streamAction, isAuthenticated])

  const candidates = useMemo(() => {
    const rawSignals = signals.data?.signals || {}
    const rawMarket = market.data?.instruments || {}

    return Object.entries(rawSignals)
      .map(([symbol, signal]) => {
        const marketState = rawMarket[symbol]

        // Only a finite, positive price is real market data. Never fabricate
        // a fallback (e.g. 0): missing/invalid LTP stays null and renders as "—".
        const isFresh =
          marketState?.data_fresh === true && Boolean(marketState?.last_tick_time)
        const liveLtp = isFresh
          ? firstValidPrice(marketState?.ltp)
          : null

        return {
          symbol,
          ltp: liveLtp,
          timestamp: signal?.timestamp || marketState?.updated_at || null,
          predictions: signal?.predictions || signal?.strategies || {},
          strategies: signal?.predictions || signal?.strategies || {},
          consensus: signal?.consensus || {
            direction: 'NEUTRAL',
            label: 'NO CONSENSUS',
            agreeing_strategies: 0,
            total_strategies: 0,
            evaluable_strategies: 0,
            consensus_agreement_pct: null,
          },
        }
      })
      .sort((a, b) => {
        const aTs = a.timestamp ? Date.parse(a.timestamp) : 0
        const bTs = b.timestamp ? Date.parse(b.timestamp) : 0
        return bTs - aTs
      })
  }, [signals.data, market.data, marketFeedFresh])

  const visible = useMemo(() => {
    if (filter === 'all') return candidates

    return candidates.filter((candidate) => {
      const pred = candidate.predictions?.[filter]
      return pred && pred.status !== 'UNAVAILABLE'
    })
  }, [candidates, filter])

  const toggle = (symbol) => {
    setExpanded((previous) => {
      const next = new Set(previous)

      if (next.has(symbol)) {
        next.delete(symbol)
      } else {
        next.add(symbol)
      }

      return next
    })
  }

  const handleStart = async () => {
    autoStartAttempts.current = 0
    setStreamAction('starting')
    setStreamError(null)

    try {
      await apiPost('/api/stream/start', {}, { requireSecret: true })
      setStreamAction('started')
      await status.refresh()
    } catch (error) {
      setStreamAction('error')
      setStreamError(error)
    }
  }

  const handleStop = async () => {
    setStreamAction('stopping')
    setStreamError(null)

    try {
      await apiPost('/api/stream/stop', {}, { requireSecret: true })
      setStreamAction('stopped')
      await status.refresh()
    } catch (error) {
      setStreamAction('error')
      setStreamError(error)
    }
  }

  const streamState = status.data?.state || 'UNKNOWN'
  const isConnected = status.data?.connected === true
  const isConnecting =
    streamState === 'CONNECTING' ||
    streamState === 'RECONNECTING' ||
    streamAction === 'starting'

  const secretError =
    isAuthenticated && !hasSharedSecret()
      ? new Error(SHARED_SECRET_MISSING_MESSAGE)
      : null
  const displayedError = streamError || secretError

  const hasSignals = candidates.length > 0
  const isAuthRequired = !isAuthenticated || status.data?.last_error?.includes('No active Zerodha Kite session')

  return (
    <div className="space-y-4 animate-fade-in">
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <StatusPill
              tone={
                isConnected
                  ? 'positive'
                  : isConnecting
                    ? 'accent'
                    : streamState === 'ERROR'
                      ? 'negative'
                      : 'neutral'
              }
            >
              {isConnected
                ? 'Live stream connected'
                : isConnecting
                  ? 'Connecting…'
                  : streamState === 'ERROR'
                    ? 'Stream error'
                    : 'Stream stopped'}
            </StatusPill>

            {signalFeedFresh ? (
              <StatusPill tone="positive">
                Real Kite signals
              </StatusPill>
            ) : signals.data ? (
              <StatusPill tone="warning">
                Signal feed stale
              </StatusPill>
            ) : null}

            {status.data?.subscribed_token_count != null && (
              <StatusPill tone="neutral">
                {status.data.subscribed_token_count} instruments
              </StatusPill>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={handleStart}
              disabled={!isAuthenticated || isConnected || isConnecting || streamAction === 'stopping'}
              className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded border border-[var(--border-strong)] text-[var(--text-dim)] hover:text-[var(--text)] hover:border-[var(--accent)] transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Play className="w-3 h-3" />
              {isConnecting ? 'Starting…' : 'Start stream'}
            </button>

            <button
              onClick={handleStop}
              disabled={!isConnected || streamAction === 'stopping'}
              className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded border border-[var(--border-strong)] text-[var(--text-dim)] hover:text-[var(--text)] hover:border-[var(--negative)] transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Square className="w-3 h-3" />
              {streamAction === 'stopping' ? 'Stopping…' : 'Stop stream'}
            </button>

            <Timestamp
              updatedAt={
                signals.updatedAt ||
                market.updatedAt ||
                status.updatedAt
              }
              staleAfterMs={10000}
            />
          </div>
        </div>
      </Card>

      {displayedError && (
        <Card>
          <ErrorState
            error={displayedError}
            onRetry={handleStart}
          />
        </Card>
      )}

      {status.status === 'loading' && !status.data ? (
        <Card>
          <Loading label="Checking live market stream…" />
        </Card>
      ) : status.status === 'error' && !status.data ? (
        <Card>
          <ErrorState
            error={status.error}
            onRetry={status.refresh}
          />
        </Card>
      ) : isAuthRequired ? (
        <Card>
          <EmptyState
            label="Live signals require a connected Kite session."
            hint="Authenticate with Kite Connect before starting the read-only market stream."
          />
        </Card>
      ) : !isConnected ? (
        <Card>
          <EmptyState
            label={
              isConnecting
                ? 'Connecting to the Kite market stream…'
                : 'Live market stream is not running.'
            }
            hint={
              status.data?.last_error ||
              'Start the stream to receive real ticks, completed 15-minute candles, and strategy signals.'
            }
          />
        </Card>
      ) : !signalFeedFresh ? (
        <Card>
          <EmptyState
            label="Live signal feed is stale."
            hint="The Kite stream is connected but fresh market data has not been confirmed. No cached signals are displayed."
          />
        </Card>
      ) : signals.status === 'loading' && !signals.data ? (
        <Card>
          <Loading label="Waiting for the first completed 15-minute candle…" />
        </Card>
      ) : signals.status === 'error' && !signals.data ? (
        <Card>
          <ErrorState
            error={signals.error}
            onRetry={signals.refresh}
          />
        </Card>
      ) : (
        <>
          <Card>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-1 rounded-md border border-[var(--border-strong)] p-1 flex-wrap">
                {STRATEGIES.map((strategy) => (
                  <button
                    key={strategy.id}
                    onClick={() => setFilter(strategy.id)}
                    className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
                      filter === strategy.id
                        ? 'bg-[var(--accent-dim)] text-[var(--accent)]'
                        : 'text-[var(--text-dim)] hover:text-[var(--text)]'
                    }`}
                  >
                    {strategy.label}
                  </button>
                ))}
              </div>

              <div className="text-xs text-[var(--text-faint)] font-num">
                {status.data?.tick_count ?? 0} ticks ·{' '}
                {status.data?.candle_count ?? 0} completed candles
              </div>
            </div>
          </Card>

          {!hasSignals ? (
            <Card>
              <EmptyState
                label="No completed live signals yet."
                hint="The system only evaluates completed 15-minute candles. No signal is generated from a forming candle."
              />
            </Card>
          ) : visible.length === 0 ? (
            <Card>
              <EmptyState
                label={`No ${labelFor(filter)} signals currently available.`}
              />
            </Card>
          ) : (
            <>
              <KeyInsights candidates={candidates} />

              <div className="space-y-3">
                {visible.map((candidate) => (
                  <SignalRow
                    key={candidate.symbol}
                    candidate={candidate}
                    filter={filter}
                    expanded={expanded.has(candidate.symbol)}
                    onToggle={() => toggle(candidate.symbol)}
                  />
                ))}
              </div>
            </>
          )}
        </>
      )}

      {/* Dynamic Strategy Performance & Signal Journal */}
      <StrategyPerformanceSection />
    </div>
  )
}

function KeyInsights({ candidates }) {
  const longs = candidates.filter(
    (candidate) => candidate.consensus?.direction === 'LONG',
  )

  const shorts = candidates.filter(
    (candidate) => candidate.consensus?.direction === 'SHORT',
  )

  const divergent = candidates.filter(
    (candidate) => candidate.consensus?.direction === 'DIVERGENT',
  )

  // Tally state counts across all strategies
  let totalLongVotes = 0
  let totalShortVotes = 0
  let waitingCount = 0
  let notApplicableCount = 0
  let unavailableCount = 0
  let staleCount = 0
  let noTradeCount = 0
  let errorCount = 0

  for (const c of candidates) {
    const preds = c.predictions || {}
    for (const pred of Object.values(preds)) {
      if (!pred) continue
      const d = pred.direction
      const s = String(pred.status || '').toUpperCase()
      if (d === 'LONG' || d === 'BUY') totalLongVotes++
      else if (d === 'SHORT' || d === 'SELL') totalShortVotes++

      if (s === 'WAITING' || s === 'MONITORING' || s === 'BUFFER_ZONE') waitingCount++
      else if (s === 'NOT_APPLICABLE') notApplicableCount++
      else if (s === 'UNAVAILABLE') unavailableCount++
      else if (s === 'STALE') staleCount++
      else if (s === 'NO_TRADE') noTradeCount++
      else if (s === 'ERROR') errorCount++
    }
  }

  // Only real LONG/SHORT consensus is eligible
  const directionalCandidates = candidates.filter(
    (candidate) =>
      candidate.consensus?.direction === 'LONG' ||
      candidate.consensus?.direction === 'SHORT',
  )

  const strongest = [...directionalCandidates].sort(
    (a, b) =>
      Number(b.consensus?.agreeing_strategies || 0) -
        Number(a.consensus?.agreeing_strategies || 0) ||
      Number(b.consensus?.consensus_agreement_pct || 0) -
        Number(a.consensus?.consensus_agreement_pct || 0),
  )[0]

  const topLong = [...longs].sort(
    (a, b) =>
      Number(b.consensus?.agreeing_strategies || 0) -
      Number(a.consensus?.agreeing_strategies || 0),
  )[0]

  const topShort = [...shorts].sort(
    (a, b) =>
      Number(b.consensus?.agreeing_strategies || 0) -
      Number(a.consensus?.agreeing_strategies || 0),
  )[0]

  const items = [
    { key: 'top-long', label: 'Long consensus', candidate: topLong, tone: 'positive' },
    { key: 'top-short', label: 'Short consensus', candidate: topShort, tone: 'negative' },
    { key: 'strongest', label: 'Strongest consensus', candidate: strongest, tone: 'accent' },
  ].filter((item) => item.candidate)

  return (
    <div className="space-y-3">
      {/* Strategy State Summary Bar */}
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-2">
        <div className="rounded-lg border border-[var(--positive)]/30 bg-[var(--positive-dim)]/40 p-2 text-center">
          <p className="text-[10px] uppercase font-semibold text-[var(--positive)]">Long Votes</p>
          <p className="text-sm font-bold font-num text-[var(--positive)]">{totalLongVotes}</p>
        </div>
        <div className="rounded-lg border border-[var(--negative)]/30 bg-[var(--negative-dim)]/40 p-2 text-center">
          <p className="text-[10px] uppercase font-semibold text-[var(--negative)]">Short Votes</p>
          <p className="text-sm font-bold font-num text-[var(--negative)]">{totalShortVotes}</p>
        </div>
        <div className="rounded-lg border border-[var(--border)] bg-white/[0.02] p-2 text-center">
          <p className="text-[10px] uppercase font-semibold text-[var(--text-faint)]">No Trade</p>
          <p className="text-sm font-bold font-num text-[var(--text-dim)]">{noTradeCount}</p>
        </div>
        <div className="rounded-lg border border-[var(--accent)]/30 bg-[var(--accent-dim)]/40 p-2 text-center">
          <p className="text-[10px] uppercase font-semibold text-[var(--accent)]">Waiting</p>
          <p className="text-sm font-bold font-num text-[var(--accent)]">{waitingCount}</p>
        </div>
        <div className="rounded-lg border border-[var(--border)] bg-white/[0.02] p-2 text-center">
          <p className="text-[10px] uppercase font-semibold text-[var(--text-faint)]">Not Applicable</p>
          <p className="text-sm font-bold font-num text-[var(--text-faint)]">{notApplicableCount}</p>
        </div>
        <div className="rounded-lg border border-[var(--warning)]/30 bg-[var(--warning-dim)]/40 p-2 text-center">
          <p className="text-[10px] uppercase font-semibold text-[var(--warning)]">Unavailable</p>
          <p className="text-sm font-bold font-num text-[var(--warning)]">{unavailableCount}</p>
        </div>
        <div className="rounded-lg border border-[var(--warning)]/30 bg-[var(--warning-dim)]/40 p-2 text-center">
          <p className="text-[10px] uppercase font-semibold text-[var(--warning)]">Stale</p>
          <p className="text-sm font-bold font-num text-[var(--warning)]">{staleCount}</p>
        </div>
        <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-2 text-center">
          <p className="text-[10px] uppercase font-semibold text-red-400">Error</p>
          <p className="text-sm font-bold font-num text-red-400">{errorCount}</p>
        </div>
      </div>

      {items.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          {items.map(({ key, label, candidate, tone }) => (
            <Card key={key}>
              <p className="text-xs text-[var(--text-faint)] mb-1">
                {label}
              </p>

              <div className="flex items-center justify-between gap-2">
                <span className="font-semibold font-num">
                  {candidate.symbol}
                </span>

                <StatusPill tone={tone}>
                  {candidate.consensus?.label || 'N/A'}
                </StatusPill>
              </div>

              <p className="text-xs text-[var(--text-dim)] mt-1 font-num">
                LTP {formatCurrency(candidate.ltp)}
              </p>
            </Card>
          ))}
        </div>
      )}

      {divergent.length > 0 && (
        <Card>
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs text-[var(--text-faint)]">
              Divergent live signals
            </p>
            <span className="text-xs font-num text-[var(--warning)]">
              {divergent.length}
            </span>
          </div>

          <div className="flex flex-wrap gap-2 mt-2">
            {divergent.map((candidate) => (
              <span
                key={candidate.symbol}
                className="inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs font-medium text-[var(--warning)] bg-[var(--warning-dim)] border-[var(--warning)]/30"
              >
                {candidate.symbol}
              </span>
            ))}
          </div>
        </Card>
      )}
    </div>
  )
}

function SignalRow({
  candidate,
  filter,
  expanded,
  onToggle,
}) {
  const preds = candidate.predictions || {}
  const focused = filter !== 'all' ? preds[filter] : null

  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <span className="font-semibold font-num text-base">
            {candidate.symbol}
          </span>

          <span className="text-sm font-num text-[var(--text-dim)]">
            {formatCurrency(candidate.ltp)}
          </span>

          <StatusPill
            tone={consensusTone(candidate.consensus?.direction)}
          >
            {candidate.consensus?.label || 'NO CONSENSUS'}
          </StatusPill>
        </div>

        <button
          onClick={onToggle}
          className="flex items-center gap-1 text-xs text-[var(--accent)] hover:underline"
        >
          {expanded ? 'Hide details' : 'View details'}

          {expanded ? (
            <ChevronUp className="w-3.5 h-3.5" />
          ) : (
            <ChevronDown className="w-3.5 h-3.5" />
          )}
        </button>
      </div>

      {filter === 'all' ? (
        <div className="mt-3 flex flex-wrap gap-2">
          {ALL_STRATEGY_KEYS.map((key) => (
            <StrategyChip
              key={key}
              name={key}
              pred={preds[key]}
            />
          ))}
        </div>
      ) : (
        focused && (
          filter === 'crsd' || focused.hedge_legs ? (
            <div className="mt-3 space-y-2">
              <div className="grid grid-cols-2 sm:grid-cols-6 gap-3">
                <MiniStat
                  label="Status"
                  value={formatStatus(focused.status)}
                  tone={dirTone(focused.direction, focused.status)}
                />
                <MiniStat
                  label="Target Action"
                  value={focused.direction || '—'}
                  tone={dirTone(focused.direction, focused.status)}
                />
                <MiniStat
                  label="Target Entry"
                  value={formatCurrency(focused.entry)}
                />
                <MiniStat
                  label="Z-Score"
                  value={focused.metrics?.z != null ? String(focused.metrics.z) : '—'}
                />
                <MiniStat
                  label="Entry / Exit Z"
                  value={focused.levels?.entry_z != null && focused.levels?.exit_z != null ? `±${focused.levels.entry_z} / ±${focused.levels.exit_z}` : '—'}
                />
                <MiniStat
                  label="Risk Scale"
                  value={focused.levels?.risk_scale != null ? `${focused.levels.risk_scale}x` : '1.0x'}
                />
              </div>

              {focused.hedge_legs && Object.keys(focused.hedge_legs).length > 0 && (
                <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-[var(--border)]/40 text-xs">
                  <span className="text-[var(--text-faint)] font-medium">Hedge basket:</span>
                  {Object.entries(focused.hedge_legs).map(([hSym, w]) => (
                    <span key={hSym} className="inline-flex items-center gap-1 px-2 py-0.5 rounded border border-[var(--border-strong)] bg-black/40 font-num">
                      <span className="font-semibold text-[var(--text)]">{hSym}</span>
                      <span className={w >= 0 ? 'text-[var(--positive)]' : 'text-[var(--negative)]'}>
                        {w >= 0 ? `+${Math.round(Math.abs(w) * 100)}%` : `-${Math.round(Math.abs(w) * 100)}%`}
                      </span>
                    </span>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <div className="mt-3 grid grid-cols-2 sm:grid-cols-5 gap-3">
              <MiniStat
                label="Status"
                value={formatStatus(focused.status)}
                tone={dirTone(focused.direction, focused.status)}
              />

              <MiniStat
                label="Direction"
                value={focused.direction || '—'}
                tone={dirTone(focused.direction, focused.status)}
              />

              <MiniStat
                label="Entry"
                value={formatCurrency(focused.entry)}
              />

              <MiniStat
                label="Stop loss"
                value={formatCurrency(focused.stop_loss)}
                tone="negative"
              />

              <MiniStat
                label="Target"
                value={formatCurrency(focused.target)}
                tone="positive"
              />
            </div>
          )
        )
      )}

      {expanded && (
        <div className="mt-4 pt-4 border-t border-[var(--border)] space-y-3">
          {ALL_STRATEGY_KEYS.map((key) => (
            <StrategyDetail
              key={key}
              name={key}
              pred={preds[key]}
              symbol={candidate.symbol}
            />
          ))}
        </div>
      )}
    </Card>
  )
}

function StrategyChip({ name, pred }) {
  if (!pred) return null

  const tone = dirTone(
    pred.direction,
    pred.status,
  )

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs font-medium ${chipTone(tone)}`}
    >
      {pred.direction === 'LONG' && (
        <ArrowUpRight className="w-3 h-3" />
      )}

      {pred.direction === 'SHORT' && (
        <ArrowDownRight className="w-3 h-3" />
      )}

      {labelFor(name)}: {formatStatus(pred.status)}
    </span>
  )
}

function StrategyDetail({ name, pred, symbol }) {
  if (!pred) return null

  if (name === 'crsd' || pred.hedge_legs) {
    const hedgeLegs = pred.hedge_legs || {}
    const zVal = pred.metrics?.z ?? pred.metrics?.residual_z
    const zCsVal = pred.metrics?.z_cs
    const entryZ = pred.levels?.entry_z
    const exitZ = pred.levels?.exit_z
    const riskScale = pred.levels?.risk_scale ?? pred.metrics?.risk_scale ?? 1.0
    const spread = pred.metrics?.spread

    return (
      <div className="rounded-lg border border-[var(--border)] p-3 space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold">
              {labelFor(name)}
            </span>
            <span className="text-xs text-[var(--accent)] font-medium px-2 py-0.5 rounded bg-[var(--accent-dim)]">
              Market-Neutral Pair
            </span>
          </div>

          <StatusPill
            tone={dirTone(pred.direction, pred.status)}
            dot={false}
          >
            {formatStatus(pred.status)}
          </StatusPill>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <MiniStat
            label="Target"
            value={pred.symbol || symbol || '—'}
          />
          <MiniStat
            label="Action"
            value={pred.direction || '—'}
            tone={dirTone(pred.direction, pred.status)}
          />
          <MiniStat
            label="Target Entry"
            value={formatCurrency(pred.entry)}
          />
          <MiniStat
            label="Risk Scale"
            value={`${riskScale}x`}
          />
        </div>

        {Object.keys(hedgeLegs).length > 0 && (
          <div className="p-2.5 rounded-lg border border-[var(--border)] bg-white/[0.02]">
            <p className="text-xs text-[var(--text-faint)] mb-1.5 font-medium">
              Hedge basket (Notional Weights):
            </p>
            <div className="flex flex-wrap gap-2">
              {Object.entries(hedgeLegs).map(([hSym, w]) => (
                <span
                  key={hSym}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded border border-[var(--border-strong)] bg-black/40 text-xs font-num"
                >
                  <span className="font-semibold text-[var(--text)]">{hSym}</span>
                  <span className={w >= 0 ? 'text-[var(--positive)] font-medium' : 'text-[var(--negative)] font-medium'}>
                    {w >= 0 ? `+${Math.round(Math.abs(w) * 100)}%` : `-${Math.round(Math.abs(w) * 100)}%`}
                  </span>
                </span>
              ))}
            </div>
          </div>
        )}

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 pt-2 border-t border-[var(--border)]/40">
          <MiniStat
            label="Z-Score"
            value={zVal != null ? String(zVal) : '—'}
            tone={zVal != null && Math.abs(zVal) >= (entryZ || 2.0) ? 'accent' : 'neutral'}
          />
          <MiniStat
            label="Cross-Sectional Z"
            value={zCsVal != null ? String(zCsVal) : '—'}
          />
          <MiniStat
            label="Entry Z"
            value={entryZ != null ? `±${entryZ}` : '—'}
          />
          <MiniStat
            label="Exit Z"
            value={exitZ != null ? `±${exitZ}` : '—'}
          />
        </div>

        {spread != null && (
          <div className="text-xs text-[var(--text-faint)] font-num">
            Residual Spread: <span className="text-[var(--text-dim)]">{formatValue(spread)}</span>
          </div>
        )}

        {pred.reason && (
          <p className="text-xs text-[var(--text-dim)]">
            {pred.reason}
          </p>
        )}
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-[var(--border)] p-3">
      <div className="flex items-center justify-between gap-3 mb-2">
        <span className="text-sm font-semibold">
          {labelFor(name)}
        </span>

        <StatusPill
          tone={dirTone(pred.direction, pred.status)}
          dot={false}
        >
          {formatStatus(pred.status)}
        </StatusPill>
      </div>

      {pred.direction && (
        <div className="grid grid-cols-3 gap-3 mb-2">
          <MiniStat
            label="Entry"
            value={formatCurrency(pred.entry)}
          />

          <MiniStat
            label="Stop loss"
            value={formatCurrency(pred.stop_loss)}
            tone="negative"
          />

          <MiniStat
            label="Target"
            value={formatCurrency(pred.target)}
            tone="positive"
          />
        </div>
      )}

      {pred.reason && (
        <p className="text-xs text-[var(--text-dim)] mb-2">
          {pred.reason}
        </p>
      )}

      {pred.levels &&
        Object.keys(pred.levels).length > 0 && (
          <div className="flex flex-wrap gap-x-4 gap-y-1 mb-1">
            {Object.entries(pred.levels).map(([key, value]) => (
              <span
                key={key}
                className="text-xs font-num text-[var(--text-faint)]"
              >
                {key}:{' '}
                <span className="text-[var(--text-dim)]">
                  {formatValue(value)}
                </span>
              </span>
            ))}
          </div>
        )}

      {pred.metrics &&
        Object.keys(pred.metrics).length > 0 && (
          <div className="flex flex-wrap gap-x-4 gap-y-1 mt-1 pt-1 border-t border-[var(--border)]/40">
            {Object.entries(pred.metrics).map(([key, value]) => (
              <span
                key={key}
                className="text-xs font-num text-[var(--text-faint)]"
              >
                {key}:{' '}
                <span className="text-[var(--accent)] font-medium">
                  {formatValue(value)}
                </span>
              </span>
            ))}
          </div>
        )}
    </div>
  )
}

function MiniStat({ label, value, tone }) {
  const color =
    tone === 'positive'
      ? 'text-[var(--positive)]'
      : tone === 'negative'
        ? 'text-[var(--negative)]'
        : tone === 'warning'
          ? 'text-[var(--warning)]'
          : tone === 'accent'
            ? 'text-[var(--accent)]'
            : 'text-[var(--text)]'

  return (
    <div>
      <p className="text-xs text-[var(--text-faint)]">
        {label}
      </p>

      <p className={`text-sm font-num font-medium ${color}`}>
        {value || '—'}
      </p>
    </div>
  )
}

function dirTone(direction, status) {
  const d = String(direction || '').toUpperCase()
  const s = String(status || '').toUpperCase()

  if (d === 'LONG' || d === 'BUY') return 'positive'
  if (d === 'SHORT' || d === 'SELL') return 'negative'

  if (s === 'WAITING' || s === 'MONITORING' || s === 'BUFFER_ZONE') {
    return 'accent'
  }

  if (s === 'NOT_APPLICABLE') {
    return 'muted'
  }

  if (s === 'UNAVAILABLE' || s === 'STALE') {
    return 'warning'
  }

  if (s === 'ERROR') {
    return 'negative'
  }

  return 'neutral'
}

function chipTone(tone) {
  if (tone === 'positive') {
    return 'text-[var(--positive)] bg-[var(--positive-dim)] border-[var(--positive)]/30'
  }

  if (tone === 'negative') {
    return 'text-[var(--negative)] bg-[var(--negative-dim)] border-[var(--negative)]/30'
  }

  if (tone === 'accent') {
    return 'text-[var(--accent)] bg-[var(--accent-dim)] border-[var(--accent)]/30'
  }

  if (tone === 'warning') {
    return 'text-[var(--warning)] bg-[var(--warning-dim)] border-[var(--warning)]/30'
  }

  if (tone === 'muted') {
    return 'text-[var(--text-faint)] bg-white/[0.02] border-[var(--border)]'
  }

  return 'text-[var(--text-dim)] bg-white/[0.04] border-[var(--border-strong)]'
}

function consensusTone(direction) {
  if (direction === 'LONG') return 'positive'
  if (direction === 'SHORT') return 'negative'
  if (direction === 'DIVERGENT') return 'warning'
  return 'neutral'
}

function formatStatus(status) {
  return status
    ? String(status).replaceAll('_', ' ')
    : 'N/A'
}

function formatValue(value) {
  if (value == null) return 'N/A'

  if (typeof value === 'number') {
    return Number.isInteger(value)
      ? String(value)
      : String(Number(value.toFixed(4)))
  }

  return String(value)
}

function firstValidPrice(...values) {
  for (const value of values) {
    if (value == null || value === '') continue
    const numeric = Number(value)
    if (Number.isFinite(numeric) && numeric > 0) return numeric
  }
  return null
}

function formatCurrency(value) {
  if (value == null || !Number.isFinite(Number(value))) {
    return '—'
  }

  return `₹${Number(value).toLocaleString('en-IN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`
}

function labelFor(key) {
  return (
    {
      orb: 'ORB',
      cpr: 'CPR',
      dual_ema: 'Dual EMA',
      apex: 'APEX-AIVEM',
      sector_impulse: 'Sector Impulse',
      ssf_l5_srm: 'SSF-L5-SRM',
      aou_oss: 'AOU-OSS',
      crsd: 'CRSD',
    }[key] || key
  )
}