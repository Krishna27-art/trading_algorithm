import React from 'react';
import { BarChart3, TrendingUp, RefreshCw, ShieldCheck, DollarSign, Activity, AlertCircle } from 'lucide-react';

export default function PerformancePage({
  backtestReport,
  backtestError,
  onRunBacktest,
  isLoading,
  selectedStrategy = 'cpr',
  selectedSymbol = 'NIFTY',
  onSelectStrategy,
}) {
  const report = backtestReport;
  const formatINR = (val) => `₹${Math.round(val || 0).toLocaleString('en-IN')}`;

  return (
    <div className="space-y-4 font-mono animate-fade-in">
      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-white/[0.08]">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="text-xl font-extrabold text-white tracking-tight flex items-center gap-2.5">
              <BarChart3 className="w-5 h-5 text-indigo-400" /> Quantitative Backtest Performance
            </h2>
            <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 uppercase">
              {selectedStrategy} • {selectedSymbol}
            </span>
          </div>
          <p className="text-xs text-slate-400 mt-0.5 font-sans">
            180-Day Institutional Event-Driven Simulation (Net of Revised Oct 2024 SEBI & Brokerage Frictions)
          </p>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={() => onRunBacktest && onRunBacktest(selectedStrategy, selectedSymbol)}
            disabled={isLoading}
            className="term-btn-primary py-2 px-4 text-xs font-mono font-bold"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${isLoading ? 'animate-spin' : ''}`} />
            <span>{isLoading ? 'Fetching Real Candles...' : `Run ${selectedStrategy.toUpperCase()} Backtest`}</span>
          </button>
        </div>
      </div>

      {/* Real-time Error Diagnostics Banner */}
      {backtestError && (
        <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-300 text-xs font-mono flex items-start gap-3 shadow-lg">
          <AlertCircle className="w-5 h-5 text-rose-400 shrink-0 mt-0.5" />
          <div className="space-y-1">
            <div className="font-extrabold text-rose-200 uppercase tracking-wider text-[11px]">
              Kite Historical Data Request Notice
            </div>
            <div className="text-slate-300 font-sans text-xs leading-relaxed">
              {backtestError}
            </div>
            <div className="text-[11px] text-slate-400 pt-1">
              • Ensure your Zerodha developer app has the paid <span className="text-white font-bold">Historical Data</span> add-on enabled.<br />
              • Check that your daily session is active via the <span className="text-white font-bold">Kite Login</span> page.
            </div>
          </div>
        </div>
      )}


      {/* When no backtest has been run */}
      {!report ? (
        <div className="term-panel p-12 text-center space-y-4">
          <BarChart3 className="w-10 h-10 mx-auto text-slate-600" />
          <div className="space-y-1">
            <h3 className="text-base font-bold text-white">No Backtest Executed Yet</h3>
            <p className="text-xs text-slate-400 max-w-md mx-auto font-sans">
              Click the button above to simulate <span className="text-indigo-400 font-bold">{selectedStrategy.toUpperCase()}</span> over 180 days of real market data for <span className="text-indigo-400 font-bold">{selectedSymbol}</span>.
            </p>
          </div>
          <button
            onClick={() => onRunBacktest && onRunBacktest(selectedStrategy, selectedSymbol)}
            disabled={isLoading}
            className="term-btn-primary py-2 px-5 text-xs font-bold inline-flex items-center gap-2"
          >
            <RefreshCw className={`w-4 h-4 ${isLoading ? 'animate-spin' : ''}`} />
            <span>{isLoading ? 'Simulating...' : 'Execute Backtest Now'}</span>
          </button>
        </div>
      ) : (
        <>
          {/* KPI Cards Row */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <div className="term-panel p-4">
              <span className="text-xs text-slate-400 font-sans block uppercase font-semibold">Net Cumulative P&L</span>
              <span className={`text-2xl font-black mt-1 block ${report.net_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                {formatINR(report.net_pnl)}
              </span>
              <span className="text-xs text-slate-400 mt-0.5 block font-sans">CAGR: +{report.cagr_pct}%</span>
            </div>

            <div className="term-panel p-4">
              <span className="text-xs text-slate-400 font-sans block uppercase font-semibold">Win Rate (Hit Ratio)</span>
              <span className="text-2xl font-black text-white mt-1 block">{report.win_rate_pct}%</span>
              <span className="text-xs text-slate-400 mt-0.5 block font-sans">
                {report.winning_trades}W / {report.losing_trades}L ({report.total_trades} Trades)
              </span>
            </div>

            <div className="term-panel p-4">
              <span className="text-xs text-slate-400 font-sans block uppercase font-semibold">Profit Factor</span>
              <span className="text-2xl font-black text-indigo-300 mt-1 block">
                {report.profit_factor ? Number(report.profit_factor).toFixed(2) : '—'}
              </span>
              <span className="text-xs text-slate-400 mt-0.5 block font-sans">
                Sharpe Ratio: {report.sharpe_ratio}
              </span>
            </div>

            <div className="term-panel p-4">
              <span className="text-xs text-slate-400 font-sans block uppercase font-semibold">Max Strategy Drawdown</span>
              <span className="text-2xl font-black text-rose-400 mt-1 block">
                -{report.max_drawdown_pct}%
              </span>
              <span className="text-xs text-slate-400 mt-0.5 block font-sans">
                Avg R: +{report.avg_r_multiple}R
              </span>
            </div>
          </div>

          {/* Detailed Statistics Table */}
          <div className="term-panel p-4 space-y-3">
            <h3 className="text-xs font-bold text-slate-200 uppercase tracking-wider">
              Complete Trade & Friction Statistics
            </h3>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-xs">
              <div>
                <span className="text-slate-400 block font-sans">Gross Strategy P&L:</span>
                <span className="text-sm font-bold text-white mt-0.5 block">{formatINR(report.gross_pnl)}</span>
              </div>
              <div>
                <span className="text-slate-400 block font-sans">Total Transaction Frictions:</span>
                <span className="text-sm font-bold text-rose-400 mt-0.5 block">-{formatINR(report.total_transaction_costs)}</span>
              </div>
              <div>
                <span className="text-slate-400 block font-sans">Expectancy per Trade:</span>
                <span className="text-sm font-bold text-emerald-400 mt-0.5 block">{formatINR(report.expectancy_rupees)}</span>
              </div>
              <div>
                <span className="text-slate-400 block font-sans">Max Consecutive Losses:</span>
                <span className="text-sm font-bold text-slate-200 mt-0.5 block">{report.max_consecutive_losses}</span>
              </div>
              <div>
                <span className="text-slate-400 block font-sans">Long Trades:</span>
                <span className="text-sm font-bold text-slate-200 mt-0.5 block">{report.long_trades} ({report.long_win_rate}%)</span>
              </div>
              <div>
                <span className="text-slate-400 block font-sans">Short Trades:</span>
                <span className="text-sm font-bold text-slate-200 mt-0.5 block">{report.short_trades} ({report.short_win_rate}%)</span>
              </div>
              <div>
                <span className="text-slate-400 block font-sans">Long Net P&L:</span>
                <span className="text-sm font-bold text-emerald-400 mt-0.5 block">{formatINR(report.long_net_pnl)}</span>
              </div>
              <div>
                <span className="text-slate-400 block font-sans">Short Net P&L:</span>
                <span className="text-sm font-bold text-emerald-400 mt-0.5 block">{formatINR(report.short_net_pnl)}</span>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
