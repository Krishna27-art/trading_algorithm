import { useState, useMemo } from 'react'
import { Search, RefreshCw, TrendingUp, TrendingDown, Layers, Activity } from 'lucide-react'
import Card from '../components/common/Card'
import StatusPill from '../components/common/StatusPill'
import Timestamp from '../components/common/Timestamp'
import { Loading, ErrorState, EmptyState } from '../components/common/DataStates'
import { usePolling } from '../hooks/usePolling'
import { getMarketPrices } from '../api/market'
import { formatCurrency, formatNumber } from '../utils/format'

function formatCompactVolume(vol) {
  if (vol === null || vol === undefined || Number.isNaN(vol) || vol === 0) return '0'
  const num = Number(vol)
  if (num >= 10000000) return `${(num / 10000000).toFixed(2)}Cr`
  if (num >= 100000) return `${(num / 100000).toFixed(2)}L`
  if (num >= 1000) return `${(num / 1000).toFixed(1)}K`
  return num.toLocaleString('en-IN')
}

function formatChangePct(val) {
  if (val === null || val === undefined || Number.isNaN(val)) return 'N/A'
  const num = Number(val)
  const sign = num > 0 ? '+' : ''
  return `${sign}${num.toFixed(2)}%`
}

function formatChangeVal(val) {
  if (val === null || val === undefined || Number.isNaN(val)) return 'N/A'
  const num = Number(val)
  const sign = num > 0 ? '+' : ''
  return `${sign}${num.toFixed(2)}`
}

export default function StocksPage({ isAuthenticated }) {
  const { data, status, error, updatedAt, refresh } = usePolling(getMarketPrices, {
    intervalMs: 5000,
  })

  const [searchQuery, setSearchQuery] = useState('')
  const [selectedCategory, setSelectedCategory] = useState('all') // 'all' | 'large' | 'mid' | 'small'

  const stocks = useMemo(() => data?.stocks || [], [data?.stocks])

  // Filter stocks by category and search
  const filteredStocks = useMemo(() => {
    return stocks.filter((stock) => {
      const matchCat = selectedCategory === 'all' || stock.category?.toLowerCase() === selectedCategory
      if (!matchCat) return false

      if (!searchQuery.trim()) return true
      const q = searchQuery.toLowerCase().trim()
      const symMatch = stock.symbol?.toLowerCase().includes(q)
      const nameMatch = stock.name?.toLowerCase().includes(q)
      return symMatch || nameMatch
    })
  }, [stocks, selectedCategory, searchQuery])

  // Aggregate summary counts
  const stats = useMemo(() => {
    let advances = 0
    let declines = 0
    let unchanged = 0
    let unavailable = 0

    stocks.forEach((s) => {
      if (s.status === 'DATA_UNAVAILABLE' || s.ltp === null) {
        unavailable++
      } else if (s.change > 0) {
        advances++
      } else if (s.change < 0) {
        declines++
      } else {
        unchanged++
      }
    })

    return {
      total: stocks.length,
      advances,
      declines,
      unchanged,
      unavailable,
    }
  }, [stocks])

  const isAuthRequired = data?.status === 'AUTH_REQUIRED' && !isAuthenticated
  const isRealKite = data?.data_source === 'REAL_KITE'

  return (
    <div className="space-y-4 animate-fade-in">
      {/* Top Header Card */}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2.5">
            <div className="flex items-center gap-2">
              <div className="h-6 w-6 rounded bg-emerald-500/10 border border-emerald-500/20 flex items-center justify-center">
                <TrendingUp className="w-3.5 h-3.5 text-emerald-400" />
              </div>
              <h1 className="text-base font-bold tracking-tight text-[var(--text)]">300 Stocks</h1>
            </div>

            {isAuthRequired ? (
              <StatusPill tone="warning">AUTH REQUIRED (Zerodha Kite)</StatusPill>
            ) : isRealKite ? (
              <StatusPill tone="positive">LIVE ● REAL_KITE</StatusPill>
            ) : (
              <StatusPill tone="neutral">{data?.data_source || 'INITIALIZING'}</StatusPill>
            )}

            <span className="text-xs text-[var(--text-faint)] font-mono">
              Universe: {stocks.length ? `${stocks.length} Constituents` : '300 Constituents'}
            </span>
          </div>

          <div className="flex items-center gap-3">
            <button
              onClick={() => refresh()}
              disabled={status === 'loading'}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-[var(--border)] bg-white/[0.03] text-xs font-medium text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-white/[0.07] transition-colors disabled:opacity-50"
              title="Refresh Quotes"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${status === 'loading' ? 'animate-spin text-[var(--accent)]' : ''}`} />
              Refresh
            </button>
            <Timestamp updatedAt={updatedAt} />
          </div>
        </div>
      </Card>

      {/* Market Breadth & Summary Cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="p-3 rounded-lg border border-[var(--border)] bg-[var(--card-bg,rgba(255,255,255,0.02))]">
          <p className="text-xs text-[var(--text-faint)]">Total Scanned</p>
          <div className="flex items-baseline gap-2 mt-1">
            <span className="text-lg font-num font-bold text-[var(--text)]">{stats.total || 300}</span>
            <span className="text-xs text-[var(--text-dim)]">100L / 100M / 100S</span>
          </div>
        </div>

        <div className="p-3 rounded-lg border border-emerald-500/20 bg-emerald-500/[0.03]">
          <p className="text-xs text-emerald-400/80">Advances</p>
          <div className="flex items-baseline gap-2 mt-1">
            <span className="text-lg font-num font-bold text-emerald-400">{stats.advances}</span>
            <span className="text-xs text-emerald-400/60 font-num">
              {stats.total ? `${((stats.advances / stats.total) * 100).toFixed(0)}%` : '0%'}
            </span>
          </div>
        </div>

        <div className="p-3 rounded-lg border border-rose-500/20 bg-rose-500/[0.03]">
          <p className="text-xs text-rose-400/80">Declines</p>
          <div className="flex items-baseline gap-2 mt-1">
            <span className="text-lg font-num font-bold text-rose-400">{stats.declines}</span>
            <span className="text-xs text-rose-400/60 font-num">
              {stats.total ? `${((stats.declines / stats.total) * 100).toFixed(0)}%` : '0%'}
            </span>
          </div>
        </div>

        <div className="p-3 rounded-lg border border-[var(--border)] bg-[var(--card-bg,rgba(255,255,255,0.02))]">
          <p className="text-xs text-[var(--text-faint)]">Unchanged / Neutral</p>
          <div className="flex items-baseline gap-2 mt-1">
            <span className="text-lg font-num font-bold text-[var(--text-dim)]">
              {stats.unchanged + stats.unavailable}
            </span>
            <span className="text-xs text-[var(--text-faint)]">
              {stats.unavailable > 0 ? `(${stats.unavailable} offline)` : 'Neutral'}
            </span>
          </div>
        </div>
      </div>

      {/* Main Stock Universe Table with Filters */}
      <Card>
        {/* Controls Bar: Search + Category Filter Tabs */}
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pb-3 border-b border-[var(--border)]">
          {/* Search Box */}
          <div className="relative flex-1 max-w-sm">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[var(--text-faint)]" />
            <input
              type="text"
              placeholder="Search symbol or company..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full pl-9 pr-3 py-1.5 text-xs bg-white/[0.04] border border-[var(--border)] rounded-md text-[var(--text)] placeholder-[var(--text-faint)] focus:outline-none focus:border-[var(--accent)] transition-colors"
            />
          </div>

          {/* Category Tabs */}
          <div className="flex items-center gap-1 bg-white/[0.03] p-1 rounded-md border border-[var(--border)]">
            {[
              { id: 'all', label: 'All (300)' },
              { id: 'large', label: 'Large (100)' },
              { id: 'mid', label: 'Mid (100)' },
              { id: 'small', label: 'Small (100)' },
            ].map((tab) => (
              <button
                key={tab.id}
                onClick={() => setSelectedCategory(tab.id)}
                className={`px-2.5 py-1 text-xs font-medium rounded transition-colors ${
                  selectedCategory === tab.id
                    ? 'bg-[var(--accent-dim)] text-[var(--accent)] font-semibold shadow-sm'
                    : 'text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-white/[0.04]'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>
        </div>

        {/* Content Table State */}
        {status === 'loading' && !data ? (
          <Loading />
        ) : error && !data ? (
          <ErrorState error={error} onRetry={refresh} />
        ) : isAuthRequired ? (
          <EmptyState
            label="Kite Authentication Required"
            hint="Please log in with Zerodha Kite in System settings to stream real live market quotes for the 300 stocks."
          />
        ) : filteredStocks.length === 0 ? (
          <EmptyState label="No matching stocks found" hint="Try adjusting your search or category filter." />
        ) : (
          <div className="overflow-x-auto mt-2">
            <table className="w-full text-xs text-left">
              <thead>
                <tr className="border-b border-[var(--border)] text-[var(--text-faint)] uppercase tracking-wider font-semibold">
                  <th className="py-2.5 pr-2 w-12 text-center">Rank</th>
                  <th className="py-2.5 pr-3">Symbol</th>
                  <th className="py-2.5 pr-3">Company</th>
                  <th className="py-2.5 pr-3">Category</th>
                  <th className="py-2.5 pr-3 text-right">LTP</th>
                  <th className="py-2.5 pr-3 text-right">Change</th>
                  <th className="py-2.5 pr-3 text-right">Chg %</th>
                  <th className="py-2.5 pr-3 text-right">Open</th>
                  <th className="py-2.5 pr-3 text-right">Volume</th>
                  <th className="py-2.5 pr-3 text-right">VWAP</th>
                  <th className="py-2.5 pl-2 text-center">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border)] font-num">
                {filteredStocks.map((stock) => {
                  const isAvailable = stock.status === 'LIVE' && stock.ltp !== null
                  const isPositive = isAvailable && stock.change > 0
                  const isNegative = isAvailable && stock.change < 0
                  const pnlClass = isPositive
                    ? 'text-emerald-400 font-semibold'
                    : isNegative
                      ? 'text-rose-400 font-semibold'
                      : 'text-[var(--text-dim)]'

                  return (
                    <tr
                      key={stock.symbol}
                      className="hover:bg-white/[0.02] transition-colors group"
                    >
                      {/* Rank */}
                      <td className="py-2.5 pr-2 text-center text-[var(--text-faint)] font-mono">
                        {stock.rank || '-'}
                      </td>

                      {/* Symbol */}
                      <td className="py-2.5 pr-3 font-sans font-bold text-[var(--text)] group-hover:text-[var(--accent)] transition-colors">
                        {stock.symbol}
                      </td>

                      {/* Company Name */}
                      <td className="py-2.5 pr-3 font-sans text-[var(--text-dim)] truncate max-w-[180px]" title={stock.name}>
                        {stock.name}
                      </td>

                      {/* Category */}
                      <td className="py-2.5 pr-3 font-sans">
                        <span
                          className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider ${
                            stock.category === 'large'
                              ? 'bg-blue-500/10 text-blue-400 border border-blue-500/20'
                              : stock.category === 'mid'
                                ? 'bg-purple-500/10 text-purple-400 border border-purple-500/20'
                                : 'bg-amber-500/10 text-amber-400 border border-amber-500/20'
                          }`}
                        >
                          {stock.category}
                        </span>
                      </td>

                      {/* LTP */}
                      <td className="py-2.5 pr-3 text-right font-semibold text-[var(--text)]">
                        {isAvailable ? formatCurrency(stock.ltp) : 'N/A'}
                      </td>

                      {/* Change */}
                      <td className={`py-2.5 pr-3 text-right ${pnlClass}`}>
                        {isAvailable ? formatChangeVal(stock.change) : 'N/A'}
                      </td>

                      {/* Change % */}
                      <td className={`py-2.5 pr-3 text-right ${pnlClass}`}>
                        {isAvailable ? (
                          <span className="inline-flex items-center gap-0.5">
                            {isPositive ? <TrendingUp className="w-3 h-3 inline" /> : isNegative ? <TrendingDown className="w-3 h-3 inline" /> : null}
                            {formatChangePct(stock.change_pct)}
                          </span>
                        ) : (
                          'N/A'
                        )}
                      </td>

                      {/* Open */}
                      <td className="py-2.5 pr-3 text-right text-[var(--text-dim)]">
                        {isAvailable && stock.open_price ? formatCurrency(stock.open_price) : 'N/A'}
                      </td>

                      {/* Volume */}
                      <td className="py-2.5 pr-3 text-right text-[var(--text-dim)]">
                        {isAvailable ? formatCompactVolume(stock.volume) : '0'}
                      </td>

                      {/* VWAP */}
                      <td className="py-2.5 pr-3 text-right text-[var(--text-dim)]">
                        {isAvailable && stock.vwap ? formatCurrency(stock.vwap) : 'N/A'}
                      </td>

                      {/* Status */}
                      <td className="py-2.5 pl-2 text-center font-sans">
                        {isAvailable ? (
                          <span className="inline-block w-2 h-2 rounded-full bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.6)]" title="Live Kite Quote" />
                        ) : (
                          <span className="inline-block w-2 h-2 rounded-full bg-zinc-600" title="Data Unavailable" />
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
