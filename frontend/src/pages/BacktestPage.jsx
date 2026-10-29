import { useState } from 'react'
import { Play, Loader2 } from 'lucide-react'
import Card from '../components/common/Card'
import StatusPill from '../components/common/StatusPill'
import { ErrorState, EmptyState } from '../components/common/DataStates'
import { runBacktest, getResearchBacktest, runWalkForwardApi } from '../api/backtest'
import { ApiError } from '../api/client'
import { formatCurrency, formatNumber } from '../utils/format'

const STRATEGIES = [
  { id: 'gap_fade', label: 'Gap Fade', usesSymbol: true },
  { id: 'rsi_ema', label: 'RSI x EMA', usesSymbol: true },
  { id: 'orb_sip', label: 'ORB Stocks-in-Play (experimental)', usesSymbol: true },
]

export default function BacktestPage() {
  const [strategy, setStrategy] = useState('gap_fade')
  const [symbol, setSymbol] = useState('NIFTY')
  const [days, setDays] = useState(180)

  const [result, setResult] = useState(null)
  const [status, setStatus] = useState('idle') // idle | loading | success | error
  const [error, setError] = useState(null)

  const [compare, setCompare] = useState(null)
  const [compareStatus, setCompareStatus] = useState('idle')

  const [wf, setWf] = useState(null)
  const [wfStatus, setWfStatus] = useState('idle')
  const [wfError, setWfError] = useState(null)

  const runWalkForward = async () => {
    setWfStatus('loading')
    setWfError(null)
    try {
      const data = await runWalkForwardApi(strategy, symbol, days)
      setWf(data)
      setWfStatus('success')
    } catch (err) {
      setWfError(err instanceof ApiError ? err : new Error('Walk-forward failed.'))
      setWfStatus('error')
    }
  }

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
  const trades = (strategy === 'rm100' || strategy === 'crsd') ? result?.trades : null
  const isCrsd = strategy === 'crsd'
  const pairInfo = result?.pair_info

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
        {selected.usesSymbol && strategy !== 'rm100' && (
          <div className="mt-3 flex items-center gap-3">
            <button
              onClick={runWalkForward}
              disabled={wfStatus === 'loading'}
              className="flex items-center gap-2 rounded-md border border-[var(--border-strong)] px-3 py-1.5 text-xs font-medium text-[var(--text-dim)] hover:text-[var(--text)] disabled:opacity-50"
            >
              {wfStatus === 'loading' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
              Walk-forward test (60-day train / 20-day test)
            </button>
            <span className="text-xs text-[var(--text-faint)]">Entries fill at the next candle's open.</span>
          </div>
        )}
      </Card>

      {wfStatus === 'error' && <Card><ErrorState error={wfError} onRetry={runWalkForward} label="Walk-forward failed." /></Card>}
      {wfStatus === 'success' && wf && <WalkForwardCard wf={wf} />}

      {status === 'error' && <Card><ErrorState error={error} onRetry={run} label="Backtest failed." /></Card>}

      {status === 'success' && report && (
        <>
          {isCrsd ? (
            <Card
              title="CRSD Pair Backtest"
              action={<DataSourceBadge source={result.data_source} />}
            >
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-4">
                <Metric label="Target" value={pairInfo?.target_symbol || symbol} />
                <Metric
                  label="Hedge basket"
                  value={
                    pairInfo?.hedge_basket && pairInfo.hedge_basket.length > 0
                      ? pairInfo.hedge_basket.join(', ')
                      : 'Sector Peers'
                  }
                />
                <Metric label="Pair trades" value={report.total_trades} />
                <Metric label="Pair win rate" value={`${formatNumber(report.win_rate_pct, 1)}%`} />
                <Metric label="Gross pair P&L" value={formatCurrency(report.gross_pnl)} tone={pnlTone(report.gross_pnl)} />
                <Metric
                  label="Total leg costs"
                  value={formatCurrency(pairInfo?.total_leg_costs ?? report.total_transaction_costs)}
                  tone="negative"
                />
                <Metric label="Net pair P&L" value={formatCurrency(report.net_pnl)} tone={pnlTone(report.net_pnl)} />
                <Metric label="Max drawdown" value={`${formatNumber(report.max_drawdown_pct, 1)}%`} tone="negative" />
              </div>

              {pairInfo && (
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 pt-3 border-t border-[var(--border)] text-xs text-[var(--text-dim)]">
                  <div>
                    <span className="text-[var(--text-faint)]">Target Gross: </span>
                    <span className={pnlTone(pairInfo.target_gross_pnl) === 'positive' ? 'text-[var(--positive)]' : ''}>
                      {formatCurrency(pairInfo.target_gross_pnl)}
                    </span>
                  </div>
                  <div>
                    <span className="text-[var(--text-faint)]">Hedge Gross: </span>
                    <span className={pnlTone(pairInfo.hedge_gross_pnl) === 'positive' ? 'text-[var(--positive)]' : ''}>
                      {formatCurrency(pairInfo.hedge_gross_pnl)}
                    </span>
                  </div>
                  <div>
                    <span className="text-[var(--text-faint)]">Target Costs: </span>
                    <span>{formatCurrency(pairInfo.target_costs)}</span>
                  </div>
                  <div>
                    <span className="text-[var(--text-faint)]">Hedge Costs: </span>
                    <span>{formatCurrency(pairInfo.hedge_costs)}</span>
                  </div>
                </div>
              )}
            </Card>
          ) : (
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
          )}

          {result?.robustness?.monte_carlo && <MonteCarloCard mc={result.robustness.monte_carlo} />}

          {trades && trades.length > 0 && <TradesAndEquity trades={trades} />}
        </>
      )}

      {status === 'success' && !report && (
        <Card><EmptyState label="Backtest completed with no result payload." /></Card>
      )}

      {selected.usesSymbol && !isCrsd && (
        <Card title="Compare intraday strategies" action={<span className="text-xs text-[var(--text-faint)]">All 7 intraday strategies, same symbol &amp; lookback</span>}>
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
                  {compare.comparison.map((row) => row.trades == null ? (
                    <tr key={row.strategy_id} className="border-b border-[var(--border)] last:border-0">
                      <td className="py-2 pr-3">{row.strategy}</td>
                      <td className="py-2 pr-3">
                        <StatusPill tone="neutral" dot={false}>{String(row.state || 'UNAVAILABLE').replace('_', ' ')}</StatusPill>
                      </td>
                      <td colSpan={6} className="py-2 pr-3 text-xs text-[var(--text-dim)]">{row.execution_note || 'Not evaluable on this data.'}</td>
                    </tr>
                  ) : (
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
              {compare.excluded_irregular_sessions?.length > 0 && (
                <p className="text-xs text-[var(--text-faint)] mt-2">
                  Excluded irregular NSE sessions (DR drill / Muhurat): {compare.excluded_irregular_sessions.join(', ')}
                </p>
              )}
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

function MonteCarloCard({ mc, title = 'Monte Carlo stress test' }) {
  if (!mc || !mc.trades) {
    return <Card title={title}><EmptyState label="No completed trades to stress-test." /></Card>
  }
  return (
    <Card title={title} action={<StatusPill tone={mc.reliable ? 'accent' : 'neutral'} dot={false}>{mc.reliable ? `${mc.simulations} runs` : 'Small sample'}</StatusPill>}>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        <Metric label="Typical max drawdown" value={`${formatNumber(mc.max_drawdown_pct_p50, 1)}%`} tone="negative" />
        <Metric label="Bad-case drawdown (95%)" value={`${formatNumber(mc.max_drawdown_pct_p95, 1)}%`} tone="negative" />
        <Metric label="Losing streak (95%)" value={mc.losing_streak_p95} />
        <Metric label="Chance of loss" value={`${formatNumber(mc.probability_of_loss_pct, 1)}%`} tone={mc.probability_of_loss_pct > 20 ? 'negative' : undefined} />
        <Metric label="P&L, bad case (5%)" value={formatCurrency(mc.net_pnl_p05)} tone={pnlTone(mc.net_pnl_p05)} />
        <Metric label="P&L, median" value={formatCurrency(mc.net_pnl_p50)} tone={pnlTone(mc.net_pnl_p50)} />
        <Metric label="P&L, good case (95%)" value={formatCurrency(mc.net_pnl_p95)} tone={pnlTone(mc.net_pnl_p95)} />
        <Metric label={`Chance of ${mc.ruin_drawdown_pct}% drawdown`} value={`${formatNumber(mc.probability_of_ruin_pct, 1)}%`} />
      </div>
      <p className="text-xs text-[var(--text-faint)] mt-3">{mc.note} Trades are reshuffled and resampled; no prices are invented.</p>
    </Card>
  )
}

function WalkForwardCard({ wf }) {
  const oos = wf.out_of_sample || {}
  return (
    <>
      <Card title={`Walk-forward: ${wf.strategy} on ${wf.symbol}`} action={<DataSourceBadge source={wf.data_source} />}>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-4">
          <Metric label="Unseen-data net P&L" value={formatCurrency(oos.net_pnl)} tone={pnlTone(oos.net_pnl)} />
          <Metric label="Unseen-data trades" value={oos.total_trades} />
          <Metric label="Unseen-data win rate" value={`${formatNumber(oos.win_rate_pct, 1)}%`} />
          <Metric label="Profitable folds" value={`${formatNumber(wf.fold_statistics?.pct_profitable_folds, 0)}%`} />
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-[var(--text-faint)] border-b border-[var(--border)]">
                <th className="py-2 pr-3 font-medium">Fold</th>
                <th className="py-2 pr-3 font-medium">Test window</th>
                <th className="py-2 pr-3 font-medium">Trades</th>
                <th className="py-2 pr-3 font-medium">Win rate</th>
                <th className="py-2 pr-3 font-medium">Net P&L</th>
              </tr>
            </thead>
            <tbody>
              {(wf.folds || []).map((f) => (
                <tr key={f.fold} className="border-b border-[var(--border)] last:border-0 font-num">
                  <td className="py-2 pr-3">{f.fold}</td>
                  <td className="py-2 pr-3">{f.test}</td>
                  <td className="py-2 pr-3">{f.trades}</td>
                  <td className="py-2 pr-3">{formatNumber(f.win_rate_pct, 1)}%</td>
                  <td className={`py-2 pr-3 ${f.net_pnl > 0 ? 'text-[var(--positive)]' : f.net_pnl < 0 ? 'text-[var(--negative)]' : ''}`}>{formatCurrency(f.net_pnl)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <MonteCarloCard mc={wf.robustness?.monte_carlo} title="Monte Carlo on unseen-data trades" />
    </>
  )
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
