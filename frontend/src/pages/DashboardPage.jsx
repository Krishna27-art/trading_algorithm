import { ArrowUpRight, ArrowDownRight } from 'lucide-react'
import Card from '../components/common/Card'
import StatusPill from '../components/common/StatusPill'
import Timestamp from '../components/common/Timestamp'
import { Loading, ErrorState, EmptyState } from '../components/common/DataStates'
import { usePolling } from '../hooks/usePolling'
import { getSystemHealth } from '../api/system'
import { getStrategyState, getTelemetry } from '../api/market'
import { getPositions } from '../api/positions'
import { formatCurrency } from '../utils/format'

export default function DashboardPage({ onNavigate }) {
  const health = usePolling(getSystemHealth, { intervalMs: 10000 })
  const state = usePolling(() => getStrategyState(), { intervalMs: 30000 })

  const symbol = state.data?.symbol || 'NIFTY'
  const strategy = state.data?.strategy_key || 'cpr'

  const telemetry = usePolling(() => getTelemetry(symbol, strategy), {
    intervalMs: 10000,
    deps: [symbol, strategy],
  })
  const positions = usePolling(getPositions, { intervalMs: 10000 })

  const overallReady = health.data?.overall_status === 'READY'

  return (
    <div className="space-y-4 animate-fade-in">
      {/* Market status */}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2.5">
            <StatusPill tone={overallReady ? 'positive' : 'negative'}>
              {health.status === 'loading' ? 'Checking backend…' : overallReady ? 'Backend connected' : 'Backend disconnected'}
            </StatusPill>
            {telemetry.data && (
              <StatusPill tone={telemetry.data.market_status === 'OPEN' ? 'positive' : 'neutral'}>
                Market {telemetry.data.market_status === 'OPEN' ? 'open' : 'closed'}
              </StatusPill>
            )}
            {telemetry.data?.market_phase && <StatusPill tone="neutral">{telemetry.data.market_phase}</StatusPill>}
            {health.data && (
              <StatusPill tone={health.data.market_data === 'CONNECTED' ? 'positive' : 'warning'}>
                Market data {health.data.market_data === 'CONNECTED' ? 'available' : 'unavailable'}
              </StatusPill>
            )}
          </div>
          <Timestamp updatedAt={telemetry.updatedAt} />
        </div>
      </Card>

      {/* Active signal */}
      <Card title="Current signal" action={<span className="text-xs text-[var(--text-faint)] font-num">{symbol} · {strategyLabel(strategy)}</span>}>
        {telemetry.status === 'loading' && !telemetry.data ? (
          <Loading />
        ) : telemetry.status === 'error' && !telemetry.data ? (
          <ErrorState error={telemetry.error} onRetry={telemetry.refresh} />
        ) : !telemetry.data?.authenticated ? (
          <EmptyState
            label="Live signals require a connected Kite session."
            hint={telemetry.data?.message}
          />
        ) : telemetry.data.active_signal ? (
          <SignalSummary signal={telemetry.data.active_signal} />
        ) : (
          <EmptyState label="No active signal" hint={telemetry.data.algorithm_state} />
        )}
      </Card>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Positions summary */}
        <Card
          title="Positions"
          action={
            <button onClick={() => onNavigate('positions')} className="text-xs text-[var(--accent)] hover:underline">
              View all
            </button>
          }
        >
          {positions.status === 'loading' && !positions.data ? (
            <Loading />
          ) : positions.status === 'error' && !positions.data ? (
            <ErrorState error={positions.error} onRetry={positions.refresh} />
          ) : !positions.data?.count ? (
            <EmptyState label="No open positions" />
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <Stat label="Open positions" value={positions.data.count} />
              <Stat
                label="Unrealized P&L"
                value={formatCurrency(positions.data.total_unrealised_pnl)}
                tone={pnlTone(positions.data.total_unrealised_pnl)}
              />
              <Stat
                label="Realized P&L"
                value={formatCurrency(positions.data.total_realised_pnl)}
                tone={pnlTone(positions.data.total_realised_pnl)}
              />
              <Stat
                label="Total P&L"
                value={formatCurrency(positions.data.total_pnl)}
                tone={pnlTone(positions.data.total_pnl)}
              />
            </div>
          )}
        </Card>

        {/* Risk summary */}
        <Card
          title="Risk today"
          action={
            <button onClick={() => onNavigate('positions')} className="text-xs text-[var(--accent)] hover:underline">
              Details
            </button>
          }
        >
          {!telemetry.data?.risk_summary ? (
            <Loading />
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <Stat label="Capital" value={formatCurrency(telemetry.data.risk_summary.capital)} />
              <Stat label="Daily risk used" value={formatCurrency(telemetry.data.risk_summary.daily_risk_used)} />
              <Stat label="Risk remaining" value={formatCurrency(telemetry.data.risk_summary.daily_risk_remaining)} />
              <Stat label="Trades today" value={`${telemetry.data.risk_summary.trades_taken} / ${telemetry.data.risk_summary.max_trades}`} />
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}

function SignalSummary({ signal }) {
  const long = signal.type === 'BUY'
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
        <Stat label="Entry" value={formatCurrency(signal.entry)} />
        <Stat label="Stop loss" value={formatCurrency(signal.stop_loss)} tone="negative" />
        <Stat label="Target" value={formatCurrency(signal.target)} tone="positive" />
        <Stat label="Confidence" value={isFinite(signal.confidence) ? `${signal.confidence}%` : 'N/A'} />
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
  return { orb: 'ORB', cpr: 'CPR', dual_ema: 'Dual EMA', rm100: 'RM100', vrp: 'VRP' }[key] || key.toUpperCase()
}
