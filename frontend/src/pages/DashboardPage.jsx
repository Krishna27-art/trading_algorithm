import { useMemo } from 'react'
import { ArrowUpRight, ArrowDownRight, Radio } from 'lucide-react'
import Card from '../components/common/Card'
import StatusPill from '../components/common/StatusPill'
import Timestamp from '../components/common/Timestamp'
import { Loading, ErrorState, EmptyState } from '../components/common/DataStates'
import { usePolling } from '../hooks/usePolling'
import { getSystemHealth } from '../api/system'
import { getStrategyState } from '../api/market'
import { apiGet } from '../api/client'
import { formatCurrency } from '../utils/format'

export default function DashboardPage({ onNavigate, isAuthenticated }) {
  const health = usePolling(getSystemHealth, { intervalMs: 10000 })
  const state = usePolling(() => getStrategyState(), { intervalMs: 30000 })

  const streamStatus = usePolling(() => apiGet('/api/stream/status'), { intervalMs: 5000 })
  const streamSignals = usePolling(() => apiGet('/api/stream/signals'), { intervalMs: 5000 })
  const streamMarket = usePolling(() => apiGet('/api/stream/market'), { intervalMs: 5000 })

  const symbol = state.data?.symbol || 'NIFTY'
  const strategy = state.data?.strategy_key || 'cpr'

  const brokerConnected = health.data?.kite_api === 'CONNECTED'
  const isStreamConnected = streamStatus.data?.connected === true
  const streamState = streamStatus.data?.state || 'DISCONNECTED'

  // Canonical active streaming signal extraction
  const activeSignal = useMemo(() => {
    const rawSignals = streamSignals.data?.signals || {}
    const rawMarket = streamMarket.data?.instruments || {}

    // Find any candidate with active strategy signals or non-neutral consensus
    const activeCandidates = []

    for (const [sym, payload] of Object.entries(rawSignals)) {
      const preds = payload?.predictions || payload?.strategies || {}
      const consensus = payload?.consensus || {}

      // Check if selected strategy or any strategy has an active signal
      let activePred = preds[strategy]

      if (
        !activePred ||
        !['LONG', 'SHORT'].includes(activePred.direction)
      ) {
        // Only scan non-CRSD directional strategies as fallback; CRSD is an independent pair strategy
        activePred = Object.entries(preds).find(
          ([stratKey, p]) =>
            stratKey !== 'crsd' &&
            p &&
            ['LONG', 'SHORT'].includes(p.direction)
        )?.[1]
      }

      if (
        activePred &&
        ['LONG', 'SHORT'].includes(activePred.direction)
      ) {
        const stratKey = activePred.strategy || activePred.strategy_key || (preds[strategy] === activePred ? strategy : 'strategy')
        activeCandidates.push({
          symbol: sym,
          strategy: stratKey,
          type:
            activePred.type ||
            (activePred.direction === 'LONG'
              ? 'BUY'
              : 'SELL'),
          direction: activePred.direction,
          entry: activePred.entry ?? null,
          stop_loss: activePred.stop_loss,
          target: activePred.target,
          hedge_legs: activePred.hedge_legs || null,
          hedge_symbol: activePred.hedge_symbol || null,
          hedge_action: activePred.hedge_action || null,
          hedge_entry: activePred.hedge_entry || null,
          levels: activePred.levels || null,
          metrics: activePred.metrics || null,
          consensus_agreement_pct: activePred.consensus_agreement_pct ?? consensus.consensus_agreement_pct,
          trigger: activePred.trigger || activePred.reason || `${sym} ${strategyLabel(stratKey)} Signal`,
          timestamp: payload?.timestamp || payload?.candle_timestamp,
        })
      } else if (
        consensus &&
        ['LONG', 'SHORT'].includes(consensus.direction)
      ) {
        activeCandidates.push({
          symbol: sym,
          strategy: 'consensus',
          type:
            consensus.direction === 'LONG'
              ? 'BUY'
              : 'SELL',
          direction: consensus.direction,
          entry: null,
          stop_loss: null,
          target: null,
          consensus_agreement_pct:
            consensus.consensus_agreement_pct,
          trigger:
            consensus.label ||
            `${consensus.agreeing_strategies}/${consensus.total_strategies} strategies agree`,
          timestamp:
            payload?.timestamp ||
            payload?.candle_timestamp,
        })
      }
    }

    // Prefer active candidate matching selected symbol if present, else top candidate
    if (activeCandidates.length > 0) {
      const matchSymbol = activeCandidates.find((c) => c.symbol === symbol)
      return matchSymbol || activeCandidates[0]
    }

    return null
  }, [streamSignals.data, streamMarket.data, symbol, strategy])

  return (
    <div className="space-y-4 animate-fade-in">
      {/* Market & Streaming status */}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2.5">
            <StatusPill tone={health.status === 'success' ? (brokerConnected ? 'positive' : 'neutral') : 'negative'}>
              {health.status === 'loading'
                ? 'Checking backend…'
                : health.status === 'success'
                  ? brokerConnected
                    ? 'Broker connected'
                    : 'Backend online'
                  : 'Backend disconnected'}
            </StatusPill>

            <StatusPill tone={isStreamConnected ? 'positive' : streamState === 'CONNECTING' ? 'warning' : 'neutral'}>
              <Radio className="w-3 h-3 mr-1 inline" />
              Stream {isStreamConnected ? 'connected' : streamState === 'CONNECTING' ? 'connecting' : 'stopped'}
            </StatusPill>

            {health.data && (
              <StatusPill tone={health.data.market_data === 'CONNECTED' ? 'positive' : 'warning'}>
                Market data {health.data.market_data === 'CONNECTED' ? 'available' : 'unavailable'}
              </StatusPill>
            )}
          </div>
          <Timestamp updatedAt={streamSignals.updatedAt || streamStatus.updatedAt} />
        </div>
      </Card>

      {/* Active canonical streaming signal */}
      <Card
        title="Current signal"
        action={
          <div className="flex items-center gap-2">
            <span className="text-xs text-[var(--text-faint)] font-num">
              {symbol} · {strategyLabel(strategy)}
            </span>
            <button onClick={() => onNavigate('signals')} className="text-xs text-[var(--accent)] hover:underline">
              Live Signals →
            </button>
          </div>
        }
      >
        {streamSignals.status === 'loading' && !streamSignals.data ? (
          <Loading />
        ) : streamSignals.status === 'error' && !streamSignals.data ? (
          <ErrorState error={streamSignals.error} onRetry={streamSignals.refresh} />
        ) : !isAuthenticated ? (
          <EmptyState label="Live streaming signals require broker login" hint="Authenticate with Zerodha Kite in System settings." />
        ) : activeSignal ? (
          <SignalSummary signal={activeSignal} />
        ) : (
          <EmptyState
            label="No active signal"
            hint={
              isStreamConnected
                ? 'Streaming pipeline active. Awaiting strategy triggers on candle close.'
                : 'Market stream stopped. Click Live Signals to start streaming.'
            }
          />
        )}
      </Card>
    </div>
  )
}

function SignalSummary({ signal }) {
  const long = signal.type === 'BUY' || signal.direction === 'LONG'
  const isCrsd = signal.strategy === 'crsd' || signal.hedge_legs != null

  if (isCrsd) {
    const hedgeLegs = signal.hedge_legs || {}
    const zScore = signal.metrics?.z ?? signal.metrics?.residual_z
    const entryZ = signal.levels?.entry_z
    const exitZ = signal.levels?.exit_z
    const riskScale = signal.levels?.risk_scale ?? signal.metrics?.risk_scale ?? 1.0

    return (
      <div className="space-y-3">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <StatusPill tone={long ? 'positive' : 'negative'} dot={false}>
              {long ? <ArrowUpRight className="w-3.5 h-3.5" /> : <ArrowDownRight className="w-3.5 h-3.5" />}
              {long ? 'BUY' : 'SELL'}
            </StatusPill>
            <span className="font-semibold">{signal.symbol}</span>
            <span className="text-xs text-[var(--accent)] font-medium px-2 py-0.5 rounded bg-[var(--accent-dim)]">
              CRSD Pair Leg
            </span>
          </div>
        </div>

        {Object.keys(hedgeLegs).length > 0 && (
          <div className="p-2.5 rounded-lg border border-[var(--border)] bg-white/[0.02]">
            <p className="text-xs text-[var(--text-faint)] mb-1.5 font-medium">Hedge Basket (Notional Weights):</p>
            <div className="flex flex-wrap gap-2">
              {Object.entries(hedgeLegs).map(([hSym, w]) => (
                <span
                  key={hSym}
                  className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded border border-[var(--border-strong)] bg-black/40 font-num"
                >
                  <span className="font-medium text-[var(--text)]">{hSym}</span>
                  <span className={w >= 0 ? 'text-[var(--positive)]' : 'text-[var(--negative)]'}>
                    {w >= 0 ? `+${Math.round(Math.abs(w) * 100)}%` : `-${Math.round(Math.abs(w) * 100)}%`}
                  </span>
                </span>
              ))}
            </div>
          </div>
        )}

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <Stat label="Target entry" value={signal.entry ? formatCurrency(signal.entry) : 'N/A'} />
          <Stat label="Z-score" value={zScore != null ? String(zScore) : 'N/A'} />
          <Stat label="Entry / Exit Z" value={entryZ != null && exitZ != null ? `±${entryZ} / ±${exitZ}` : 'N/A'} />
          <Stat label="Risk scale" value={`${riskScale}x`} />
        </div>
        {signal.trigger && <p className="text-xs text-[var(--text-dim)]">{signal.trigger}</p>}
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <StatusPill tone={long ? 'positive' : 'negative'} dot={false}>
          {long ? <ArrowUpRight className="w-3.5 h-3.5" /> : <ArrowDownRight className="w-3.5 h-3.5" />}
          {long ? 'LONG' : 'SHORT'}
        </StatusPill>
        <span className="font-semibold">{signal.symbol}</span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Stat label="Entry" value={signal.entry ? formatCurrency(signal.entry) : 'N/A'} />
        <Stat label="Stop loss" value={signal.stop_loss ? formatCurrency(signal.stop_loss) : 'N/A'} tone="negative" />
        <Stat label="Target" value={signal.target ? formatCurrency(signal.target) : 'N/A'} tone="positive" />
        <Stat
          label="Consensus agreement"
          value={
            Number.isFinite(signal.consensus_agreement_pct)
              ? `${signal.consensus_agreement_pct}%`
              : 'N/A'
          }
        />
      </div>
      {signal.trigger && <p className="text-xs text-[var(--text-dim)]">{signal.trigger}</p>}
    </div>
  )
}

function Stat({ label, value, tone }) {
  const color =
    tone === 'positive' ? 'text-[var(--positive)]' : tone === 'negative' ? 'text-[var(--negative)]' : 'text-[var(--text)]'
  return (
    <div>
      <p className="text-xs text-[var(--text-faint)]">{label}</p>
      <p className={`text-sm font-num font-semibold ${color}`}>{value}</p>
    </div>
  )
}

function strategyLabel(key) {
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
      rm100: 'RM100',
      vrp: 'VRP',
    }[key] || (key ? key.toUpperCase() : 'STRATEGY')
  )
}
