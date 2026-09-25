import { useState } from 'react'
import { LogOut } from 'lucide-react'
import Card from '../components/common/Card'
import StatusPill from '../components/common/StatusPill'
import Timestamp from '../components/common/Timestamp'
import { Loading, ErrorState, EmptyState } from '../components/common/DataStates'
import { usePolling } from '../hooks/usePolling'
import { getPositions, exitOrder } from '../api/positions'
import { getStrategyState, getTelemetry } from '../api/market'
import { ApiError, hasSharedSecret } from '../api/client'
import { formatCurrency } from '../utils/format'

export default function PositionsPage({ tradingMode }) {
  const positions = usePolling(getPositions, { intervalMs: 10000 })
  const state = usePolling(() => getStrategyState(), { intervalMs: 30000 })
  const symbol = state.data?.symbol || 'NIFTY'
  const strategy = state.data?.strategy_key || 'cpr'
  const telemetry = usePolling(() => getTelemetry(symbol, strategy), { intervalMs: 10000, deps: [symbol, strategy] })

  const [exiting, setExiting] = useState(null)
  const [confirmTarget, setConfirmTarget] = useState(null)
  const [actionError, setActionError] = useState('')

  const handleExit = async (posSymbol) => {
    setExiting(posSymbol)
    setActionError('')
    try {
      await exitOrder(posSymbol, tradingMode)
      await positions.refresh()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Exit failed.')
    } finally {
      setExiting(null)
      setConfirmTarget(null)
    }
  }

  return (
    <div className="space-y-4 animate-fade-in">
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <StatusPill tone={positions.data?.broker === 'LIVE' ? 'negative' : 'accent'}>
              {positions.data?.broker || tradingMode} broker
            </StatusPill>
          </div>
          <Timestamp updatedAt={positions.updatedAt} />
        </div>
      </Card>

      {actionError && (
        <div className="rounded-md border border-[var(--negative)]/30 bg-[var(--negative-dim)] px-3 py-2 text-sm text-[var(--negative)]">
          {actionError}
        </div>
      )}

      <Card title="Open positions">
        {positions.status === 'loading' && !positions.data ? (
          <Loading />
        ) : positions.status === 'error' && !positions.data ? (
          <ErrorState error={positions.error} onRetry={positions.refresh} />
        ) : !positions.data?.positions?.length ? (
          <EmptyState label="No open positions" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-[var(--text-faint)] border-b border-[var(--border)]">
                  <th className="py-2 pr-3 font-medium">Symbol</th>
                  <th className="py-2 pr-3 font-medium">Side</th>
                  <th className="py-2 pr-3 font-medium">Qty</th>
                  <th className="py-2 pr-3 font-medium">Entry</th>
                  <th className="py-2 pr-3 font-medium">Current</th>
                  <th className="py-2 pr-3 font-medium">Unrealized P&L</th>
                  <th className="py-2 pr-3 font-medium">Stop</th>
                  <th className="py-2 pr-3 font-medium">Target</th>
                  <th className="py-2 pr-3 font-medium">Status</th>
                  <th className="py-2 pr-3 font-medium"></th>
                </tr>
              </thead>
              <tbody>
                {positions.data.positions.map((p) => (
                  <tr key={p.position_id} className="border-b border-[var(--border)] last:border-0 font-num">
                    <td className="py-2 pr-3 font-sans font-medium">{p.tradingsymbol}</td>
                    <td className="py-2 pr-3">
                      <StatusPill tone={p.quantity >= 0 ? 'positive' : 'negative'} dot={false}>
                        {p.quantity >= 0 ? 'LONG' : 'SHORT'}
                      </StatusPill>
                    </td>
                    <td className="py-2 pr-3">{Math.abs(p.quantity)}</td>
                    <td className="py-2 pr-3">{formatCurrency(p.average_price)}</td>
                    <td className="py-2 pr-3">{formatCurrency(p.last_price)}</td>
                    <td className={`py-2 pr-3 font-medium ${p.pnl > 0 ? 'text-[var(--positive)]' : p.pnl < 0 ? 'text-[var(--negative)]' : ''}`}>
                      {formatCurrency(p.unrealised_pnl)}
                    </td>
                    <td className="py-2 pr-3">{p.strategy_stop_loss ? formatCurrency(p.strategy_stop_loss) : 'N/A'}</td>
                    <td className="py-2 pr-3">{p.strategy_target ? formatCurrency(p.strategy_target) : 'N/A'}</td>
                    <td className="py-2 pr-3 font-sans">{p.status}</td>
                    <td className="py-2 pr-3 font-sans">
                      <button
                        onClick={() => setConfirmTarget(p.tradingsymbol)}
                        disabled={exiting === p.tradingsymbol}
                        className="flex items-center gap-1 text-xs text-[var(--negative)] hover:underline disabled:opacity-40"
                      >
                        <LogOut className="w-3.5 h-3.5" />
                        {exiting === p.tradingsymbol ? 'Exiting…' : 'Exit'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {positions.data && (
          <div className="mt-4 pt-4 border-t border-[var(--border)] grid grid-cols-3 gap-3">
            <Total label="Unrealized" value={positions.data.total_unrealised_pnl} />
            <Total label="Realized" value={positions.data.total_realised_pnl} />
            <Total label="Total P&L" value={positions.data.total_pnl} />
          </div>
        )}
      </Card>

      <Card title="Risk summary">
        {!telemetry.data?.risk_summary ? (
          <Loading />
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <Total label="Capital" value={telemetry.data.risk_summary.capital} plain />
            <Total label="Daily risk limit" value={telemetry.data.risk_summary.daily_risk_limit} plain />
            <Total label="Risk used" value={telemetry.data.risk_summary.daily_risk_used} tone="negative" />
            <Total label="Risk remaining" value={telemetry.data.risk_summary.daily_risk_remaining} tone="positive" />
            <div>
              <p className="text-xs text-[var(--text-faint)]">Risk / trade</p>
              <p className="text-sm font-num font-medium">{telemetry.data.risk_summary.risk_per_trade_pct}%</p>
            </div>
            <div>
              <p className="text-xs text-[var(--text-faint)]">Kill switch</p>
              <p className="text-sm font-num font-medium">{telemetry.data.risk_summary.kill_switch_pct}%</p>
            </div>
            <div>
              <p className="text-xs text-[var(--text-faint)]">Trades today</p>
              <p className="text-sm font-num font-medium">
                {telemetry.data.risk_summary.trades_taken} / {telemetry.data.risk_summary.max_trades}
              </p>
            </div>
          </div>
        )}
      </Card>

      {confirmTarget && (
        <ConfirmExitDialog
          symbol={confirmTarget}
          tradingMode={tradingMode}
          hasSecret={hasSharedSecret()}
          onCancel={() => setConfirmTarget(null)}
          onConfirm={() => handleExit(confirmTarget)}
        />
      )}
    </div>
  )
}

function Total({ label, value, tone, plain }) {
  const color = plain
    ? 'text-[var(--text)]'
    : tone === 'negative'
      ? 'text-[var(--negative)]'
      : value > 0
        ? 'text-[var(--positive)]'
        : value < 0
          ? 'text-[var(--negative)]'
          : 'text-[var(--text)]'
  return (
    <div>
      <p className="text-xs text-[var(--text-faint)]">{label}</p>
      <p className={`text-sm font-num font-semibold ${color}`}>{formatCurrency(value)}</p>
    </div>
  )
}

function ConfirmExitDialog({ symbol, tradingMode, hasSecret, onCancel, onConfirm }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onCancel}>
      <div
        className="w-full max-w-sm rounded-xl border border-[var(--border-strong)] bg-[var(--panel)] p-5 space-y-4"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-sm font-semibold">Square off {symbol}?</h3>
        <p className="text-xs text-[var(--text-dim)]">
          This immediately closes the open position on {symbol} in {tradingMode} mode via the backend.
        </p>
        {!hasSecret && (
          <p className="text-xs text-[var(--warning)]">
            No shared secret is set — the backend will reject this until you add one in Settings.
          </p>
        )}
        <div className="flex gap-2">
          <button
            onClick={onCancel}
            className="flex-1 rounded-md border border-[var(--border-strong)] py-2 text-sm font-medium hover:bg-white/[0.05]"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className="flex-1 rounded-md bg-[var(--negative-dim)] border border-[var(--negative)]/30 text-[var(--negative)] py-2 text-sm font-medium hover:bg-[var(--negative)]/20"
          >
            Confirm exit
          </button>
        </div>
      </div>
    </div>
  )
}
