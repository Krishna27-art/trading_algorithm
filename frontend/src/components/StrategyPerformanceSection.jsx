import { useState } from 'react'
import {
  TrendingUp,
  TrendingDown,
  BarChart2,
  Clock,
  Target,
  ChevronDown,
  ChevronUp,
  RefreshCw,
  List,
  Activity,
  Layers,
} from 'lucide-react'
import Card from './common/Card'
import StatusPill from './common/StatusPill'
import { Loading, ErrorState, EmptyState } from './common/DataStates'
import { usePolling } from '../hooks/usePolling'
import { apiGet } from '../api/client'

const PERIODS = [
  { id: 'TODAY', label: 'Today' },
  { id: 'WEEK', label: 'This Week' },
  { id: 'MONTH', label: 'This Month' },
  { id: 'YEAR', label: 'This Year' },
]

export default function StrategyPerformanceSection() {
  const [period, setPeriod] = useState('TODAY')
  const [selectedStrategy, setSelectedStrategy] = useState(null)
  const [viewMode, setViewMode] = useState('summary') // 'summary' | 'journal'

  const performance = usePolling(
    () => apiGet(`/api/strategy/performance?period=${period}${selectedStrategy ? `&strategy=${selectedStrategy}` : ''}`),
    { intervalMs: 10000, deps: [period, selectedStrategy] }
  )

  const journal = usePolling(
    () => apiGet(`/api/strategy/journal?period=${period}${selectedStrategy ? `&strategy=${selectedStrategy}` : ''}&limit=50`),
    { intervalMs: 10000, deps: [period, selectedStrategy] }
  )

  const perfData = performance.data
  const strategies = perfData?.strategies || []
  const journalSignals = journal.data?.signals || []

  // Active strategy detail for drill-down view
  const activeStratDetail = selectedStrategy
    ? strategies.find((s) => s.strategy === selectedStrategy) || strategies[0]
    : null

  return (
    <Card className="p-5 flex flex-col gap-5 border border-[var(--border)] bg-[var(--panel)]">
      {/* Header and Controls */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 pb-3 border-b border-[var(--border)]">
        <div>
          <div className="flex items-center gap-2">
            <Activity className="w-5 h-5 text-[var(--accent)]" />
            <h2 className="text-lg font-semibold tracking-tight text-[var(--text)]">
              Strategy Performance & Signal Journal
            </h2>
          </div>
          <p className="text-xs text-[var(--text-dim)] mt-0.5">
            Dynamic SQLite journal tracking genuine entry signals, real target/stop breaches, and session expirations.
          </p>
        </div>

        {/* Period Switcher & View Toggle */}
        <div className="flex items-center gap-2 flex-wrap">
          <div className="inline-flex rounded-lg bg-[var(--bg)] p-1 border border-[var(--border)]">
            {PERIODS.map((p) => (
              <button
                key={p.id}
                onClick={() => setPeriod(p.id)}
                className={`px-3 py-1 text-xs font-medium rounded-md transition-colors ${
                  period === p.id
                    ? 'bg-[var(--accent)] text-black shadow-sm font-semibold'
                    : 'text-[var(--text-dim)] hover:text-[var(--text)]'
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>

          <div className="inline-flex rounded-lg bg-[var(--bg)] p-1 border border-[var(--border)]">
            <button
              onClick={() => setViewMode('summary')}
              className={`px-2.5 py-1 text-xs flex items-center gap-1 rounded-md transition-colors ${
                viewMode === 'summary'
                  ? 'bg-[var(--panel-raised)] text-[var(--text)] font-semibold'
                  : 'text-[var(--text-dim)] hover:text-[var(--text)]'
              }`}
            >
              <BarChart2 className="w-3.5 h-3.5" />
              Overview
            </button>
            <button
              onClick={() => setViewMode('journal')}
              className={`px-2.5 py-1 text-xs flex items-center gap-1 rounded-md transition-colors ${
                viewMode === 'journal'
                  ? 'bg-[var(--panel-raised)] text-[var(--text)] font-semibold'
                  : 'text-[var(--text-dim)] hover:text-[var(--text)]'
              }`}
            >
              <List className="w-3.5 h-3.5" />
              Journal Logs
            </button>
          </div>

          <button
            onClick={() => {
              performance.refresh()
              journal.refresh()
            }}
            title="Refresh statistics"
            className="p-1.5 rounded-lg bg-[var(--bg)] hover:bg-[var(--panel-raised)] text-[var(--text-dim)] hover:text-[var(--text)] border border-[var(--border)] transition-colors"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {performance.status === 'loading' && !perfData ? (
        <Loading label="Aggregating signal events from database…" />
      ) : performance.status === 'error' ? (
        <ErrorState
          error={performance.error}
          onRetry={() => performance.refresh()}
        />
      ) : (
        <>
          {/* Top Aggregate KPI Cards */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <div className="p-3 rounded-lg bg-[var(--panel-raised)] border border-[var(--border)]">
              <div className="text-[11px] uppercase tracking-wider text-[var(--text-dim)]">Total Signals</div>
              <div className="text-xl font-bold font-num text-[var(--text)] mt-1">
                {perfData?.total_signals ?? 0}
              </div>
              <div className="text-[10px] text-[var(--text-dim)] mt-0.5">
                Period: {PERIODS.find((p) => p.id === period)?.label}
              </div>
            </div>

            <div className="p-3 rounded-lg bg-[var(--panel-raised)] border border-[var(--border)]">
              <div className="text-[11px] uppercase tracking-wider text-[var(--text-dim)]">Wins / Losses</div>
              <div className="text-xl font-bold font-num mt-1 flex items-baseline gap-1.5">
                <span className="text-[var(--positive)]">{perfData?.total_wins ?? 0}W</span>
                <span className="text-[var(--text-dim)]">/</span>
                <span className="text-[var(--negative)]">{perfData?.total_losses ?? 0}L</span>
              </div>
              <div className="text-[10px] text-[var(--text-dim)] mt-0.5">
                Target breached before stop
              </div>
            </div>

            <div className="p-3 rounded-lg bg-[var(--panel-raised)] border border-[var(--border)]">
              <div className="text-[11px] uppercase tracking-wider text-[var(--text-dim)]">Expired at 15:30</div>
              <div className="text-xl font-bold font-num text-[var(--warning)] mt-1">
                {perfData?.total_expired ?? 0}
              </div>
              <div className="text-[10px] text-[var(--text-dim)] mt-0.5">
                Neither target nor stop reached
              </div>
            </div>

            <div className="p-3 rounded-lg bg-[var(--panel-raised)] border border-[var(--border)]">
              <div className="text-[11px] uppercase tracking-wider text-[var(--text-dim)]">Overall Win Rate</div>
              <div className="text-xl font-bold font-num text-[var(--accent)] mt-1">
                {perfData?.overall_accuracy != null ? `${perfData.overall_accuracy.toFixed(1)}%` : '0.0%'}
              </div>
              <div className="text-[10px] text-[var(--text-dim)] mt-0.5">
                Wins / (Wins + Losses)
              </div>
            </div>
          </div>

          {viewMode === 'summary' ? (
            <div className="flex flex-col gap-4">
              {/* Strategy Comparison Table */}
              <div className="overflow-x-auto rounded-lg border border-[var(--border)]">
                <table className="w-full text-left text-xs">
                  <thead className="bg-[var(--panel-raised)] text-[var(--text-dim)] uppercase text-[10px] tracking-wider border-b border-[var(--border)]">
                    <tr>
                      <th className="py-2.5 px-3">Strategy</th>
                      <th className="py-2.5 px-3 text-right">Signals</th>
                      <th className="py-2.5 px-3 text-right">Wins</th>
                      <th className="py-2.5 px-3 text-right">Losses</th>
                      <th className="py-2.5 px-3 text-right">Expired</th>
                      <th className="py-2.5 px-3 text-right font-semibold text-[var(--text)]">Accuracy</th>
                      <th className="py-2.5 px-3 text-right">Avg MFE</th>
                      <th className="py-2.5 px-3 text-right">Avg MAE</th>
                      <th className="py-2.5 px-3 text-right">Action</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border)]">
                    {strategies.length === 0 ? (
                      <tr>
                        <td colSpan={9} className="py-6 text-center text-[var(--text-dim)]">
                          No signals recorded for this period yet.
                        </td>
                      </tr>
                    ) : (
                      strategies.map((strat) => {
                        const isSelected = selectedStrategy === strat.strategy
                        const accColor =
                          strat.accuracy >= 65
                            ? 'text-[var(--positive)]'
                            : strat.accuracy >= 50
                            ? 'text-[var(--warning)]'
                            : 'text-[var(--text-dim)]'

                        return (
                          <tr
                            key={strat.strategy}
                            onClick={() =>
                              setSelectedStrategy(isSelected ? null : strat.strategy)
                            }
                            className={`cursor-pointer transition-colors hover:bg-[var(--panel-raised)] ${
                              isSelected ? 'bg-[var(--panel-raised)] font-medium' : ''
                            }`}
                          >
                            <td className="py-2.5 px-3">
                              <div className="font-semibold text-[var(--text)]">
                                {strat.name || strat.strategy.toUpperCase()}
                              </div>
                              <div className="text-[10px] text-[var(--text-dim)] uppercase">
                                {strat.strategy}
                              </div>
                            </td>
                            <td className="py-2.5 px-3 text-right font-num text-[var(--text)]">
                              {strat.signals}
                            </td>
                            <td className="py-2.5 px-3 text-right font-num text-[var(--positive)]">
                              {strat.wins}
                            </td>
                            <td className="py-2.5 px-3 text-right font-num text-[var(--negative)]">
                              {strat.losses}
                            </td>
                            <td className="py-2.5 px-3 text-right font-num text-[var(--warning)]">
                              {strat.expired}
                            </td>
                            <td className={`py-2.5 px-3 text-right font-num font-bold ${accColor}`}>
                              {strat.signals > 0 ? `${strat.accuracy.toFixed(1)}%` : '—'}
                            </td>
                            <td className="py-2.5 px-3 text-right font-num text-[var(--positive)]">
                              {strat.avg_mfe > 0 ? `+${strat.avg_mfe.toFixed(2)}%` : '0.00%'}
                            </td>
                            <td className="py-2.5 px-3 text-right font-num text-[var(--negative)]">
                              {strat.avg_mae < 0 ? `${strat.avg_mae.toFixed(2)}%` : '0.00%'}
                            </td>
                            <td className="py-2.5 px-3 text-right text-[var(--accent)]">
                              {isSelected ? (
                                <ChevronUp className="w-4 h-4 inline" />
                              ) : (
                                <ChevronDown className="w-4 h-4 inline opacity-60" />
                              )}
                            </td>
                          </tr>
                        )
                      })
                    )}
                  </tbody>
                </table>
              </div>

              {/* Strategy Drilldown Details Card (when selected) */}
              {activeStratDetail && (
                <div className="p-4 rounded-lg bg-[var(--panel-raised)] border border-[var(--border-strong)] flex flex-col gap-4 animate-fade-in">
                  <div className="flex items-center justify-between border-b border-[var(--border)] pb-2.5">
                    <div>
                      <h3 className="text-sm font-semibold text-[var(--text)] flex items-center gap-2">
                        <span>{activeStratDetail.name || activeStratDetail.strategy.toUpperCase()}</span>
                        <span className="text-xs px-2 py-0.5 rounded bg-[var(--bg)] text-[var(--accent)] border border-[var(--border)] font-normal">
                          {PERIODS.find((p) => p.id === period)?.label}
                        </span>
                      </h3>
                      <p className="text-xs text-[var(--text-dim)] mt-0.5">
                        {activeStratDetail.description || 'Deep statistical breakdown and stock performance.'}
                      </p>
                    </div>
                    <button
                      onClick={() => setSelectedStrategy(null)}
                      className="text-xs text-[var(--text-dim)] hover:text-[var(--text)] underline"
                    >
                      Close Breakdown
                    </button>
                  </div>

                  {/* Metric Pills Grid */}
                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                    <div className="p-2.5 rounded bg-[var(--bg)] border border-[var(--border)]">
                      <div className="text-[10px] text-[var(--text-dim)]">Win Rate</div>
                      <div className="text-base font-bold font-num text-[var(--accent)] mt-0.5">
                        {activeStratDetail.accuracy.toFixed(1)}%
                      </div>
                      <div className="text-[10px] text-[var(--text-dim)]">
                        {activeStratDetail.wins} wins / {activeStratDetail.losses} losses
                      </div>
                    </div>

                    <div className="p-2.5 rounded bg-[var(--bg)] border border-[var(--border)]">
                      <div className="text-[10px] text-[var(--text-dim)]">Long vs Short Accuracy</div>
                      <div className="text-xs font-num font-semibold text-[var(--text)] mt-1 flex flex-col gap-0.5">
                        <span className="text-[var(--positive)]">
                          LONG: {activeStratDetail.long_accuracy.toFixed(1)}% ({activeStratDetail.long_wins}/{activeStratDetail.long_signals})
                        </span>
                        <span className="text-[var(--warning)]">
                          SHORT: {activeStratDetail.short_accuracy.toFixed(1)}% ({activeStratDetail.short_wins}/{activeStratDetail.short_signals})
                        </span>
                      </div>
                    </div>

                    <div className="p-2.5 rounded bg-[var(--bg)] border border-[var(--border)]">
                      <div className="text-[10px] text-[var(--text-dim)]">Average Excursion</div>
                      <div className="text-xs font-num font-semibold mt-1 flex flex-col gap-0.5">
                        <span className="text-[var(--positive)]">
                          Avg MFE: +{activeStratDetail.avg_mfe.toFixed(2)}%
                        </span>
                        <span className="text-[var(--negative)]">
                          Avg MAE: {activeStratDetail.avg_mae.toFixed(2)}%
                        </span>
                      </div>
                    </div>

                    <div className="p-2.5 rounded bg-[var(--bg)] border border-[var(--border)]">
                      <div className="text-[10px] text-[var(--text-dim)]">Average Return / Signal</div>
                      <div className={`text-base font-bold font-num mt-0.5 ${
                        activeStratDetail.avg_return >= 0 ? 'text-[var(--positive)]' : 'text-[var(--negative)]'
                      }`}>
                        {activeStratDetail.avg_return >= 0 ? `+${activeStratDetail.avg_return.toFixed(2)}%` : `${activeStratDetail.avg_return.toFixed(2)}%`}
                      </div>
                      <div className="text-[10px] text-[var(--text-dim)]">
                        Includes closed + expired
                      </div>
                    </div>
                  </div>

                  {/* Stock-by-stock breakdown table */}
                  {activeStratDetail.symbol_breakdown && activeStratDetail.symbol_breakdown.length > 0 && (
                    <div className="mt-1 flex flex-col gap-2">
                      <div className="text-xs font-semibold text-[var(--text)] flex items-center gap-1.5">
                        <Target className="w-3.5 h-3.5 text-[var(--accent)]" />
                        Stock-Level Performance for {activeStratDetail.strategy.toUpperCase()}
                      </div>
                      <div className="overflow-x-auto rounded border border-[var(--border)]">
                        <table className="w-full text-left text-xs">
                          <thead className="bg-[var(--bg)] text-[var(--text-dim)] text-[10px] uppercase">
                            <tr>
                              <th className="py-2 px-2.5">Symbol</th>
                              <th className="py-2 px-2.5 text-right">Signals</th>
                              <th className="py-2 px-2.5 text-right">Wins</th>
                              <th className="py-2 px-2.5 text-right">Losses</th>
                              <th className="py-2 px-2.5 text-right">Expired</th>
                              <th className="py-2 px-2.5 text-right font-semibold">Accuracy</th>
                              <th className="py-2 px-2.5 text-right">Avg MFE</th>
                              <th className="py-2 px-2.5 text-right">Avg MAE</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-[var(--border)] bg-[var(--panel)]">
                            {activeStratDetail.symbol_breakdown.map((sym) => (
                              <tr key={sym.symbol} className="hover:bg-[var(--panel-raised)]">
                                <td className="py-1.5 px-2.5 font-semibold text-[var(--text)]">
                                  {sym.symbol}
                                </td>
                                <td className="py-1.5 px-2.5 text-right font-num">{sym.signals}</td>
                                <td className="py-1.5 px-2.5 text-right font-num text-[var(--positive)]">{sym.wins}</td>
                                <td className="py-1.5 px-2.5 text-right font-num text-[var(--negative)]">{sym.losses}</td>
                                <td className="py-1.5 px-2.5 text-right font-num text-[var(--warning)]">{sym.expired}</td>
                                <td className="py-1.5 px-2.5 text-right font-num font-bold text-[var(--accent)]">
                                  {sym.signals > 0 ? `${sym.accuracy.toFixed(1)}%` : '—'}
                                </td>
                                <td className="py-1.5 px-2.5 text-right font-num text-[var(--positive)]">
                                  +{sym.avg_mfe.toFixed(2)}%
                                </td>
                                <td className="py-1.5 px-2.5 text-right font-num text-[var(--negative)]">
                                  {sym.avg_mae.toFixed(2)}%
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          ) : (
            /* Journal Logs View */
            <div className="flex flex-col gap-3">
              <div className="text-xs text-[var(--text-dim)] flex items-center justify-between">
                <span>Showing up to 50 recent signals recorded in SQLite</span>
                <span className="font-num">{journalSignals.length} entries</span>
              </div>
              <div className="overflow-x-auto rounded-lg border border-[var(--border)]">
                <table className="w-full text-left text-xs">
                  <thead className="bg-[var(--panel-raised)] text-[var(--text-dim)] uppercase text-[10px] tracking-wider border-b border-[var(--border)]">
                    <tr>
                      <th className="py-2.5 px-3">Time</th>
                      <th className="py-2.5 px-3">Symbol</th>
                      <th className="py-2.5 px-3">Strategy</th>
                      <th className="py-2.5 px-3">Direction</th>
                      <th className="py-2.5 px-3 text-right">Entry</th>
                      <th className="py-2.5 px-3 text-right">Stop</th>
                      <th className="py-2.5 px-3 text-right">Target</th>
                      <th className="py-2.5 px-3 text-center">Outcome</th>
                      <th className="py-2.5 px-3 text-right">Return</th>
                      <th className="py-2.5 px-3 text-right">MFE / MAE</th>
                      <th className="py-2.5 px-3 text-right">Mins</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border)]">
                    {journalSignals.length === 0 ? (
                      <tr>
                        <td colSpan={11} className="py-6 text-center text-[var(--text-dim)]">
                          No signal journal entries recorded yet.
                        </td>
                      </tr>
                    ) : (
                      journalSignals.map((sig) => {
                        const timeStr = sig.generated_at
                          ? new Date(sig.generated_at).toLocaleTimeString('en-IN', {
                              hour: '2-digit',
                              minute: '2-digit',
                              second: '2-digit',
                              hour12: false,
                            })
                          : '—'

                        const outcomeVariant =
                          sig.outcome === 'WIN'
                            ? 'success'
                            : sig.outcome === 'LOSS'
                            ? 'danger'
                            : sig.outcome === 'EXPIRED'
                            ? 'warning'
                            : sig.outcome === 'AMBIGUOUS'
                            ? 'neutral'
                            : 'neutral'

                        return (
                          <tr key={sig.signal_id} className="hover:bg-[var(--panel-raised)]">
                            <td className="py-2 px-3 font-num text-[var(--text-dim)] text-[11px]">
                              {timeStr}
                            </td>
                            <td className="py-2 px-3 font-bold text-[var(--text)]">
                              {sig.symbol}
                            </td>
                            <td className="py-2 px-3 uppercase text-[11px] text-[var(--text-dim)]">
                              {sig.strategy}
                            </td>
                            <td className="py-2 px-3">
                              <span
                                className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                                  sig.direction === 'LONG'
                                    ? 'bg-[var(--positive-dim)] text-[var(--positive)]'
                                    : 'bg-[var(--negative-dim)] text-[var(--negative)]'
                                }`}
                              >
                                {sig.direction}
                              </span>
                            </td>
                            <td className="py-2 px-3 text-right font-num text-[var(--text)]">
                              ₹{sig.entry_price?.toFixed(2)}
                            </td>
                            <td className="py-2 px-3 text-right font-num text-[var(--negative)]">
                              ₹{sig.stop_loss?.toFixed(2)}
                            </td>
                            <td className="py-2 px-3 text-right font-num text-[var(--positive)]">
                              ₹{sig.target?.toFixed(2)}
                            </td>
                            <td className="py-2 px-3 text-center">
                              <StatusPill status={sig.outcome} variant={outcomeVariant} />
                            </td>
                            <td className="py-2 px-3 text-right font-num font-semibold">
                              {sig.return_pct != null ? (
                                <span
                                  className={
                                    sig.return_pct > 0
                                      ? 'text-[var(--positive)]'
                                      : sig.return_pct < 0
                                      ? 'text-[var(--negative)]'
                                      : 'text-[var(--text-dim)]'
                                  }
                                >
                                  {sig.return_pct > 0 ? `+${sig.return_pct.toFixed(2)}%` : `${sig.return_pct.toFixed(2)}%`}
                                </span>
                              ) : (
                                '—'
                              )}
                            </td>
                            <td className="py-2 px-3 text-right font-num text-[11px] text-[var(--text-dim)]">
                              <span className="text-[var(--positive)]">+{sig.mfe_pct?.toFixed(1)}%</span>
                              {' / '}
                              <span className="text-[var(--negative)]">{sig.mae_pct?.toFixed(1)}%</span>
                            </td>
                            <td className="py-2 px-3 text-right font-num text-[var(--text-dim)] text-[11px]">
                              {sig.minutes_to_outcome != null ? `${sig.minutes_to_outcome}m` : '—'}
                            </td>
                          </tr>
                        )
                      })
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </Card>
  )
}
