import { ArrowUpRight, ArrowDownRight } from 'lucide-react'
import Card from '../components/common/Card'
import StatusPill from '../components/common/StatusPill'
import Timestamp from '../components/common/Timestamp'
import { Loading, ErrorState, EmptyState } from '../components/common/DataStates'
import { usePolling } from '../hooks/usePolling'
import { getSystemHealth } from '../api/system'
import { getStrategyState, getTelemetry } from '../api/market'
import { getTrades } from '../api/trades'
import { formatCurrency } from '../utils/format'

export default function DashboardPage({ onNavigate, isAuthenticated }) {
  const health = usePolling(getSystemHealth, { intervalMs: 10000 })
  const state = usePolling(() => getStrategyState(), { intervalMs: 30000 })

  const symbol = state.data?.symbol || 'NIFTY'
  const strategy = state.data?.strategy_key || 'cpr'

  const telemetry = usePolling(() => getTelemetry(symbol, strategy), {
    intervalMs: 10000,
    deps: [symbol, strategy],
  })
  const trades = usePolling(getTrades, { intervalMs: 15000 })
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

  // Trust App-level auth (confirmed via /kite/status) unless the telemetry
  // endpoint explicitly reports authenticated === false.
  const kiteConnected = telemetry.data
    ? telemetry.data.authenticated !== false
    : isAuthenticated

  return (
    <div className="space-y-4 animate-fade-in">
      {/* Market status */}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2.5">
            <StatusPill tone={health.status === 'success' ? (overallReady ? 'positive' : 'neutral') : 'negative'}>
              {health.status === 'loading'
                ? 'Checking backend…'
                : health.status === 'success'
                  ? overallReady
                    ? 'Broker connected'
                    : 'Backend online (Offline mode)'
                  : 'Backend disconnected'}
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
        ) : !kiteConnected ? (
          <EmptyState
            label="Live signals require a connected Kite session."
            hint={telemetry.data?.message}
          />
        ) : telemetry.data?.active_signal ? (
          <SignalSummary signal={telemetry.data.active_signal} />
        ) : (
          <EmptyState label="No active signal" hint={telemetry.data?.algorithm_state} />
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
              <Stat
                label="Total P&L"
                value={formatCurrency(totalPnl)}
                tone={pnlTone(totalPnl)}
              />
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
      rm100: 'RM100',
      vrp: 'VRP',
    }[key] || key.toUpperCase()
  )
}
