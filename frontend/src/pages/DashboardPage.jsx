import { useMemo } from 'react'
import { ArrowUpRight, ArrowDownRight, Radio } from 'lucide-react'
import Card from '../components/common/Card'
import StatusPill from '../components/common/StatusPill'
import Timestamp from '../components/common/Timestamp'
import { Loading, ErrorState, EmptyState } from '../components/common/DataStates'
import { usePolling } from '../hooks/usePolling'
import { getSystemHealth } from '../api/system'
import { getStrategyState, getRiskSummary } from '../api/market'
import { getTrades } from '../api/trades'
import { apiGet } from '../api/client'
import { formatCurrency } from '../utils/format'

export default function DashboardPage({ onNavigate, isAuthenticated }) {
  const health = usePolling(getSystemHealth, { intervalMs: 10000 })
  const state = usePolling(() => getStrategyState(), { intervalMs: 30000 })
  const riskSummary = usePolling(getRiskSummary, { intervalMs: 10000 })
  const trades = usePolling(getTrades, { intervalMs: 15000 })

  const streamStatus = usePolling(() => apiGet('/api/stream/status'), { intervalMs: 5000 })
  const streamSignals = usePolling(() => apiGet('/api/stream/signals'), { intervalMs: 5000 })
  const streamMarket = usePolling(() => apiGet('/api/stream/market'), { intervalMs: 5000 })

  const symbol = state.data?.symbol || 'NIFTY'
  const strategy = state.data?.strategy_key || 'cpr'

  const tradesList = Array.isArray(trades.data?.trades)
    ? trades.data.trades
    : Array.isArray(trades.data)
      ? trades.data
      : []

  const totalPnl = tradesList
    .filter((t) => t.pnl_net !== null && t.pnl_net !== undefined)
    .reduce((sum, t) => sum + (t.pnl_net || 0), 0)
  const winCount = tradesList.filter((t) => (t.pnl_net || 0) > 0).length
  const lossCount = tradesList.filter((t) => (t.pnl_net || 0) < 0).length

  const overallReady = health.data?.overall_status === 'READY'
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
      const liveLtp = rawMarket[sym]?.ltp || payload?.ltp

      // Check if selected strategy or any strategy has an active signal
      let activePred = preds[strategy]
      if (!activePred || activePred.status !== 'SIGNAL') {
        // Search across all strategies for an active signal
        activePred = Object.values(preds).find(
          (p) => p && (p.status === 'SIGNAL' || p.type === 'BUY' || p.type === 'SELL' || p.direction === 'LONG' || p.direction === 'SHORT')
        )
      }

      if (activePred && (activePred.status === 'SIGNAL' || activePred.type || activePred.direction)) {
        activeCandidates.push({
          symbol: sym,
          type: activePred.type || (activePred.direction === 'LONG' ? 'BUY' : 'SELL'),
          entry: activePred.entry || liveLtp || 0,
          stop_loss: activePred.stop_loss,
          target: activePred.target,
          consensus_agreement_pct: activePred.consensus_agreement_pct ?? consensus.consensus_agreement_pct,
          trigger: activePred.trigger || activePred.reason || `${sym} ${strategyLabel(activePred.strategy_key || strategy)} Signal`,
          timestamp: payload?.timestamp || payload?.candle_timestamp,
        })
      } else if (consensus && consensus.direction && consensus.direction !== 'NEUTRAL') {
        activeCandidates.push({
          symbol: sym,
          type: consensus.direction === 'LONG' || consensus.direction === 'BUY' ? 'BUY' : 'SELL',
          entry: liveLtp || 0,
          stop_loss: null,
          target: null,
          consensus_agreement_pct: consensus.consensus_agreement_pct,
          trigger: consensus.label || `${consensus.agreeing_strategies}/${consensus.total_strategies} strategies agree`,
          timestamp: payload?.timestamp || payload?.candle_timestamp,
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

  const riskData = riskSummary.data?.risk_summary

  return (
    <div className="space-y-4 animate-fade-in">
      {/* Market & Streaming status */}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2.5">
            <StatusPill tone={health.status === 'success' ? (overallReady ? 'positive' : 'neutral') : 'negative'}>
              {health.status === 'loading'
                ? 'Checking backend…'
                : health.status === 'success'
                  ? overallReady
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

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Trade Journal summary */}
        <Card
          title="Trade Journal"
          action={
            <button onClick={() => onNavigate('trades')} className="text-xs text-[var(--accent)] hover:underline">
              View journal
            </button>
          }
        >
          {trades.status === 'loading' && !trades.data ? (
            <Loading />
          ) : trades.status === 'error' && !trades.data ? (
            <ErrorState error={trades.error} onRetry={trades.refresh} />
          ) : !tradesList.length ? (
            <EmptyState label="No journaled trades yet" hint="Strategy signals and executions are logged here" />
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <Stat label="Total trades" value={tradesList.length} />
              <Stat label="Total P&L" value={formatCurrency(totalPnl)} tone={pnlTone(totalPnl)} />
              <Stat label="Profitable" value={winCount} tone="positive" />
              <Stat label="Losses" value={lossCount} tone={lossCount > 0 ? 'negative' : undefined} />
            </div>
          )}
        </Card>

        {/* Risk summary */}
        <Card
          title="Risk today"
          action={
            <button onClick={() => onNavigate('signals')} className="text-xs text-[var(--accent)] hover:underline">
              Signals
            </button>
          }
        >
          {!riskData ? (
            <Loading />
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <Stat label="Capital" value={formatCurrency(riskData.capital)} />
              <Stat label="Daily risk used" value={formatCurrency(riskData.daily_risk_used)} />
              <Stat label="Risk remaining" value={formatCurrency(riskData.daily_risk_remaining)} />
              <Stat label="Trades today" value={`${riskData.trades_taken} / ${riskData.max_trades}`} />
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}

function SignalSummary({ signal }) {
  const long = signal.type === 'BUY' || signal.direction === 'LONG'
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

function pnlTone(v) {
  if (v > 0) return 'positive'
  if (v < 0) return 'negative'
  return undefined
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
      rm100: 'RM100',
      vrp: 'VRP',
    }[key] || (key ? key.toUpperCase() : 'STRATEGY')
  )
}
