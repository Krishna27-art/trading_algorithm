import { useState } from 'react'
import { Play, Loader2 } from 'lucide-react'
import Card from '../components/common/Card'
import StatusPill from '../components/common/StatusPill'
import { ErrorState, EmptyState } from '../components/common/DataStates'
import { runBacktest, getResearchBacktest } from '../api/backtest'
import { ApiError } from '../api/client'
import { formatCurrency, formatNumber } from '../utils/format'

const STRATEGIES = [
  { id: 'orb', label: 'ORB', usesSymbol: true },
  { id: 'cpr', label: 'CPR', usesSymbol: true },
  { id: 'dual_ema', label: 'Dual EMA', usesSymbol: true },
  { id: 'rm100', label: 'RM100 (portfolio)', usesSymbol: false },
]

export default function BacktestPage() {
  const [strategy, setStrategy] = useState('orb')
  const [symbol, setSymbol] = useState('NIFTY')
  const [days, setDays] = useState(180)

  const [result, setResult] = useState(null)
  const [status, setStatus] = useState('idle') // idle | loading | success | error
  const [error, setError] = useState(null)

  const [compare, setCompare] = useState(null)
  const [compareStatus, setCompareStatus] = useState('idle')

  const selected = STRATEGIES.find((s) => s.id === strategy)

  const run = async () => {
    setStatus('loading')
    setError(null)
    try {
      const data = await runBacktest(strategy, symbol, days)
      setResult(data)
      setStatus('success')
    } catch (err) {
      setError(err instanceof ApiError ? err : new Error('Backtest failed.'))
      setStatus('error')
    }
  }

  const runCompare = async () => {
    setCompareStatus('loading')
    try {
      const data = await getResearchBacktest(days, symbol)
      setCompare(data)
      setCompareStatus('success')
    } catch {
      setCompareStatus('error')
    }
  }

  const report = result?.report
  const trades = strategy === 'rm100' ? result?.trades : null

  return (
    <div className="space-y-4 animate-fade-in">
      <Card title="Run a backtest">
        <div className="grid grid-cols-1 sm:grid-cols-4 gap-3 items-end">
          <div className="space-y-1">
            <label className="text-xs font-medium text-[var(--text-dim)]">Strategy</label>
            <select
              value={strategy}
              onChange={(e) => setStrategy(e.target.value)}
              className="w-full rounded-md bg-black/30 border border-[var(--border-strong)] px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
            >
              {STRATEGIES.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>

          {selected.usesSymbol && (
            <div className="space-y-1">
              <label className="text-xs font-medium text-[var(--text-dim)]">Symbol</label>
              <input
                value={symbol}
                onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                className="w-full rounded-md bg-black/30 border border-[var(--border-strong)] px-3 py-2 text-sm font-num outline-none focus:border-[var(--accent)]"
              />
            </div>
          )}

          <div className="space-y-1">
            <label className="text-xs font-medium text-[var(--text-dim)]">Lookback (days)</label>
            <input
              type="number"
              min={30}
              max={730}
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              className="w-full rounded-md bg-black/30 border border-[var(--border-strong)] px-3 py-2 text-sm font-num outline-none focus:border-[var(--accent)]"
            />
          </div>

          <button
            onClick={run}
            disabled={status === 'loading'}
            className="flex items-center justify-center gap-2 rounded-md bg-[var(--accent-dim)] border border-[var(--accent)]/30 text-[var(--accent)] py-2 text-sm font-medium hover:bg-[var(--accent)]/20 disabled:opacity-50"
          >
            {status === 'loading' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
            Run backtest
          </button>
        </div>
      </Card>

      {status === 'error' && <Card><ErrorState error={error} onRetry={run} label="Backtest failed." /></Card>}

      {status === 'success' && report && (
        <>
          <Card
            title="Results"
            action={<DataSourceBadge source={result.data_source} />}
          >
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
              <Metric label="Net P&L" value={formatCurrency(report.net_pnl)} tone={pnlTone(report.net_pnl)} />
              <Metric label="Win rate" value={`${formatNumber(report.win_rate_pct, 1)}%`} />
              <Metric label="Total trades" value={report.total_trades} />
              <Metric label="Max drawdown" value={`${formatNumber(report.max_drawdown_pct, 1)}%`} tone="negative" />
              <Metric label="Profit factor" value={formatNumber(report.profit_factor, 2)} />
              <Metric label="Sharpe ratio" value={formatNumber(report.sharpe_ratio, 2)} />
              <Metric label="CAGR" value={report.cagr_pct !== undefined ? `${formatNumber(report.cagr_pct, 1)}%` : 'N/A'} />
              <Metric label="Expectancy" value={report.expectancy_rupees !== undefined ? formatCurrency(report.expectancy_rupees) : 'N/A'} />
            </div>
            <p className="text-xs text-[var(--text-faint)] mt-4">
              Final equity isn't returned by this endpoint, so it isn't shown here — see the report note below.
            </p>
          </Card>

          {trades && trades.length > 0 && <TradesAndEquity trades={trades} />}
        </>
      )}

      {status === 'success' && !report && (
        <Card><EmptyState label="Backtest completed with no result payload." /></Card>
      )}

      {selected.usesSymbol && (
        <Card title="Compare intraday strategies" action={<span className="text-xs text-[var(--text-faint)]">ORB vs CPR vs Dual EMA, same symbol &amp; lookback</span>}>
          {!compare ? (
            <button
              onClick={runCompare}
              disabled={compareStatus === 'loading'}
              className="text-xs text-[var(--accent)] hover:underline disabled:opacity-50"
            >
              {compareStatus === 'loading' ? 'Running comparison…' : 'Run comparison'}
            </button>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-[var(--text-faint)] border-b border-[var(--border)]">
                    <th className="py-2 pr-3 font-medium">Strategy</th>
                    <th className="py-2 pr-3 font-medium">State</th>
                    <th className="py-2 pr-3 font-medium">Trades</th>
                    <th className="py-2 pr-3 font-medium">Win rate</th>
                    <th className="py-2 pr-3 font-medium">Profit factor</th>
                    <th className="py-2 pr-3 font-medium">Sharpe</th>
                    <th className="py-2 pr-3 font-medium">Max DD</th>
                    <th className="py-2 pr-3 font-medium">Net P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {compare.comparison.map((row) => (
                    <tr key={row.strategy_id} className="border-b border-[var(--border)] last:border-0 font-num">
                      <td className="py-2 pr-3 font-sans">{row.strategy}</td>
                      <td className="py-2 pr-3 font-sans">
                        <StatusPill tone={row.state === 'ACTIVE' ? 'accent' : 'neutral'} dot={false}>{row.state}</StatusPill>
                      </td>
                      <td className="py-2 pr-3">{row.trades}</td>
                      <td className="py-2 pr-3">{row.win_rate}%</td>
                      <td className="py-2 pr-3">{row.profit_factor}</td>
                      <td className="py-2 pr-3">{row.sharpe}</td>
                      <td className="py-2 pr-3">{row.max_drawdown}%</td>
                      <td className={`py-2 pr-3 ${pnlTone(row.net_pnl) === 'positive' ? 'text-[var(--positive)]' : row.net_pnl < 0 ? 'text-[var(--negative)]' : ''}`}>
                        {formatCurrency(row.net_pnl)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </div>
  )
}

function DataSourceBadge({ source }) {
  return <StatusPill tone={source === 'REAL_KITE' ? 'positive' : 'warning'}>{source}</StatusPill>
}

function Metric({ label, value, tone }) {
  const color = tone === 'positive' ? 'text-[var(--positive)]' : tone === 'negative' ? 'text-[var(--negative)]' : 'text-[var(--text)]'
  return (
    <div>
      <p className="text-xs text-[var(--text-faint)]">{label}</p>
      <p className={`text-lg font-num font-semibold ${color}`}>{value}</p>
    </div>
  )
}

function pnlTone(v) {
  if (v > 0) return 'positive'
  if (v < 0) return 'negative'
  return undefined
}

// Real per-trade net_pnl values from the backend, plotted as a cumulative
// line. This is a rendering of backend numbers, not a computed prediction.
function TradesAndEquity({ trades }) {
  const sorted = [...trades].sort((a, b) => new Date(a.entry_time) - new Date(b.entry_time))
  let running = 0
  const points = sorted.map((t) => {
    running += Number(t.net_pnl) || 0
    return running
  })
  const w = 600
  const h = 140
  const min = Math.min(0, ...points)
  const max = Math.max(0, ...points)
  const range = max - min || 1
  const path = points
    .map((p, i) => {
      const x = (i / Math.max(points.length - 1, 1)) * w
      const y = h - ((p - min) / range) * h
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')

  return (
    <Card title="Cumulative P&L across trades">
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-36" preserveAspectRatio="none">
        <line x1="0" x2={w} y1={h - ((0 - min) / range) * h} y2={h - ((0 - min) / range) * h} stroke="var(--border-strong)" strokeWidth="1" />
        <path d={path} fill="none" stroke="var(--accent)" strokeWidth="2" />
      </svg>
      <div className="overflow-x-auto mt-4">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-[var(--text-faint)] border-b border-[var(--border)]">
              <th className="py-2 pr-3 font-medium">Symbol</th>
              <th className="py-2 pr-3 font-medium">Direction</th>
              <th className="py-2 pr-3 font-medium">Entry</th>
              <th className="py-2 pr-3 font-medium">Exit</th>
              <th className="py-2 pr-3 font-medium">Net P&L</th>
            </tr>
          </thead>
          <tbody>
            {sorted.slice(-25).map((t, i) => (
              <tr key={i} className="border-b border-[var(--border)] last:border-0 font-num">
                <td className="py-2 pr-3 font-sans">{t.symbol}</td>
                <td className="py-2 pr-3 font-sans">{t.direction}</td>
                <td className="py-2 pr-3">{formatCurrency(t.entry_price)}</td>
                <td className="py-2 pr-3">{formatCurrency(t.exit_price)}</td>
                <td className={`py-2 pr-3 ${t.net_pnl > 0 ? 'text-[var(--positive)]' : t.net_pnl < 0 ? 'text-[var(--negative)]' : ''}`}>
                  {formatCurrency(t.net_pnl)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {sorted.length > 25 && (
          <p className="text-xs text-[var(--text-faint)] mt-2">Showing the most recent 25 of {sorted.length} trades.</p>
        )}
      </div>
    </Card>
  )
}
