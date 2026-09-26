import Card from '../components/common/Card'
import StatusPill from '../components/common/StatusPill'
import Timestamp from '../components/common/Timestamp'
import { Loading, ErrorState } from '../components/common/DataStates'
import { usePolling } from '../hooks/usePolling'
import { getSystemHealth } from '../api/system'
import { getStatus } from '../api/auth'
import { getStrategyState } from '../api/market'

const HEALTH_ROWS = [
  { key: 'kite_api', label: 'Kite API' },
  { key: 'market_data', label: 'Market data' },
  { key: 'database', label: 'Database' },
  { key: 'strategy_engine', label: 'Strategy engine' },
  { key: 'risk_engine', label: 'Risk engine' },
  { key: 'order_manager', label: 'Order manager' },
]

export default function SystemStatusPage() {
  const health = usePolling(getSystemHealth, { intervalMs: 10000 })
  const auth = usePolling(getStatus, { intervalMs: 15000 })
  const state = usePolling(() => getStrategyState(), { intervalMs: 30000 })

  return (
    <div className="space-y-4 animate-fade-in">
      <Card
        title="Diagnostics"
        action={<Timestamp updatedAt={health.updatedAt} />}
      >
        {health.status === 'loading' && !health.data ? (
          <Loading />
        ) : health.status === 'error' && !health.data ? (
          <ErrorState error={health.error} onRetry={health.refresh} />
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {HEALTH_ROWS.map((row) => (
              <Row key={row.key} label={row.label} value={health.data[row.key]} />
            ))}
            <Row label="Active broker" value={health.data.active_broker} tone={health.data.active_broker === 'LIVE' ? 'negative' : 'accent'} />
            <Row
              label="Overall status"
              value={health.data.overall_status}
              tone={health.data.overall_status === 'READY' ? 'positive' : 'negative'}
            />
          </div>
        )}
      </Card>

      <Card title="Kite session">
        {auth.status === 'loading' && !auth.data ? (
          <Loading />
        ) : auth.status === 'error' && !auth.data ? (
          <ErrorState error={auth.error} onRetry={auth.refresh} />
        ) : (
          <div className="space-y-2">
            <Row
              label="Authentication"
              value={auth.data.connected || auth.data.authenticated ? 'CONNECTED' : 'DISCONNECTED'}
              tone={auth.data.connected || auth.data.authenticated ? 'positive' : 'negative'}
            />
            {(auth.data.user_id || auth.data.user) && (
              <>
                <Row label="User ID" value={auth.data.user_id || auth.data.user?.user_id} plain />
                <Row label="User Name" value={auth.data.user_name || auth.data.user?.user_name} plain />
                {auth.data.products?.length > 0 && (
                  <Row label="Products" value={auth.data.products.join(', ')} plain />
                )}
                {auth.data.exchanges?.length > 0 && (
                  <Row label="Exchanges" value={auth.data.exchanges.join(', ')} plain />
                )}
              </>
            )}
            {!(auth.data.connected || auth.data.authenticated) && auth.data.message && (
              <p className="text-xs text-[var(--text-dim)] mt-1">{auth.data.message}</p>
            )}
          </div>
        )}
      </Card>

      <Card title="Active strategy configuration">
        {state.status === 'loading' && !state.data ? (
          <Loading />
        ) : state.status === 'error' && !state.data ? (
          <ErrorState error={state.error} onRetry={state.refresh} />
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <Row label="Strategy" value={state.data.strategy_name} plain span />
            <Row label="Symbol" value={`${state.data.symbol} · ${state.data.exchange}`} plain />
            <Row label="Session phase" value={state.data.session_phase} plain />
            <Row label="Risk : reward" value={`1 : ${state.data.risk_reward_ratio}`} plain />
            <Row label="Risk / trade" value={`${state.data.risk_per_trade_pct}%`} plain />
            <Row label="Max daily loss" value={`${state.data.max_daily_loss_pct}%`} plain />
            <Row label="Mode" value={state.data.is_paper_trading ? 'PAPER' : 'LIVE'} tone={state.data.is_paper_trading ? 'accent' : 'negative'} />
            <Row label="Square-off" value={state.data.schedule?.square_off_time} plain />
          </div>
        )}
      </Card>
    </div>
  )
}

function Row({ label, value, tone, plain, span }) {
  return (
    <div className={span ? 'col-span-2' : ''}>
      <p className="text-xs text-[var(--text-faint)]">{label}</p>
      {plain || !tone ? (
        <p className="text-sm font-num font-medium">{value ?? 'N/A'}</p>
      ) : (
        <StatusPill tone={tone} dot={false}>
          {value}
        </StatusPill>
      )}
    </div>
  )
}
