import React, { useState } from 'react';
import {
  BarChart3,
  RefreshCw,
  TrendingUp,
  TrendingDown,
  ShieldCheck,
  Percent,
  DollarSign,
  Activity,
  Layers,
} from 'lucide-react';

export default function MultiStrategyBacktestPanel({
  backtestData,
  isLoading,
  onRunBacktest,
  selectedSymbol = 'NIFTY',
}) {
  const [days, setDays] = useState(180);

  const formatINR = (val) => {
    if (val === null || val === undefined) return '—';
    const num = Number(val);
    const prefix = num >= 0 ? '₹' : '-₹';
    return `${prefix}${Math.abs(Math.round(num)).toLocaleString('en-IN')}`;
  };

  const formatPct = (val) => {
    if (val === null || val === undefined) return '—';
    return `${Number(val).toFixed(1)}%`;
  };

  const strategies = backtestData?.strategies || {};
  const comparison = backtestData?.comparison || [];
  const orb = strategies.orb;
  const cpr = strategies.cpr;
  const dual = strategies.dual_ema;

  const renderStrategyCard = (title, sub, stratReport, stratKey) => {
    if (!stratReport) {
      return (
        <div className="p-5 rounded-xl bg-[#0e1422] border border-white/5 space-y-4">
          <div className="pb-3 border-b border-white/[0.06]">
            <h4 className="font-bold text-white text-sm font-mono">{title}</h4>
            <span className="text-xs text-slate-400">{sub}</span>
          </div>
          <div className="py-12 text-center text-xs text-slate-500 font-mono">
            {isLoading ? 'Running institutional backtest...' : 'Click "Run 180-Day Backtest" to compute metrics'}
          </div>
        </div>
      );
    }

    const isProfitable = stratReport.net_pnl > 0;

    return (
      <div className="p-5 rounded-xl bg-[#0e1422] border border-white/10 shadow-lg space-y-4 relative overflow-hidden">
        {/* Accent strip */}
        <div
          className={`absolute top-0 left-0 right-0 h-1 ${
            isProfitable ? 'bg-emerald-500' : 'bg-rose-500'
          }`}
        />

        {/* Card Header */}
        <div className="flex items-start justify-between pb-3 border-b border-white/[0.08]">
          <div>
            <h4 className="font-bold text-white text-sm tracking-wide font-mono">{title}</h4>
            <p className="text-xs text-slate-400 mt-0.5">{sub}</p>
          </div>
          <span
            className={`px-2.5 py-1 rounded text-xs font-mono font-bold ${
              isProfitable
                ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30'
                : 'bg-rose-500/10 text-rose-400 border border-rose-500/30'
            }`}
          >
            Net {formatINR(stratReport.net_pnl)}
          </span>
        </div>

        {/* Primary KPIs Grid */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5 text-center font-mono">
          <div className="p-2.5 rounded-lg bg-white/[0.02] border border-white/5">
            <span className="text-[10px] text-slate-400 uppercase tracking-wider block font-sans">
              Win Rate
            </span>
            <span className="text-base font-bold text-white mt-0.5 block">
              {formatPct(stratReport.win_rate_pct)}
            </span>
          </div>

          <div className="p-2.5 rounded-lg bg-white/[0.02] border border-white/5">
            <span className="text-[10px] text-slate-400 uppercase tracking-wider block font-sans">
              Profit Factor
            </span>
            <span
              className={`text-base font-bold mt-0.5 block ${
                stratReport.profit_factor >= 1.25
                  ? 'text-emerald-400'
                  : stratReport.profit_factor >= 1.0
                  ? 'text-amber-400'
                  : 'text-rose-400'
              }`}
            >
              {stratReport.profit_factor?.toFixed(2) || '—'}
            </span>
          </div>

          <div className="p-2.5 rounded-lg bg-white/[0.02] border border-white/5">
            <span className="text-[10px] text-slate-400 uppercase tracking-wider block font-sans">
              Sharpe Ratio
            </span>
            <span className="text-base font-bold text-indigo-300 mt-0.5 block">
              {stratReport.sharpe_ratio?.toFixed(2) || '—'}
            </span>
          </div>

          <div className="p-2.5 rounded-lg bg-white/[0.02] border border-white/5">
            <span className="text-[10px] text-slate-400 uppercase tracking-wider block font-sans">
              Max Drawdown
            </span>
            <span className="text-base font-bold text-rose-400 mt-0.5 block">
              {formatPct(stratReport.max_drawdown_pct)}
            </span>
          </div>
        </div>

        {/* Complete 18-Metric Breakdown Table */}
        <div className="space-y-1.5 text-xs font-mono border-t border-white/[0.06] pt-3">
          <div className="flex justify-between py-1 border-b border-white/[0.03]">
            <span className="text-slate-400">Total / Win / Loss Trades:</span>
            <span className="text-slate-200 font-semibold">
              {stratReport.total_trades} ({stratReport.winning_trades}W / {stratReport.losing_trades}L)
            </span>
          </div>

          <div className="flex justify-between py-1 border-b border-white/[0.03]">
            <span className="text-slate-400">Long Trades / Win Rate:</span>
            <span className="text-slate-200 font-semibold">
              {stratReport.long_trades} ({formatPct(stratReport.long_win_rate)})
            </span>
          </div>

          <div className="flex justify-between py-1 border-b border-white/[0.03]">
            <span className="text-slate-400">Short Trades / Win Rate:</span>
            <span className="text-slate-200 font-semibold">
              {stratReport.short_trades} ({formatPct(stratReport.short_win_rate)})
            </span>
          </div>

          <div className="flex justify-between py-1 border-b border-white/[0.03]">
            <span className="text-slate-400">Gross P&L:</span>
            <span className="text-slate-200 font-semibold">{formatINR(stratReport.gross_pnl)}</span>
          </div>

          <div className="flex justify-between py-1 border-b border-white/[0.03]">
            <span className="text-slate-400">Total Transaction Costs:</span>
            <span className="text-amber-400 font-semibold">
              {formatINR(stratReport.total_transaction_costs)}
            </span>
          </div>

          <div className="flex justify-between py-1 border-b border-white/[0.03]">
            <span className="text-slate-400">Net Realized P&L:</span>
            <span
              className={`font-bold ${isProfitable ? 'text-emerald-400' : 'text-rose-400'}`}
            >
              {formatINR(stratReport.net_pnl)}
            </span>
          </div>

          <div className="flex justify-between py-1 border-b border-white/[0.03]">
            <span className="text-slate-400">CAGR %:</span>
            <span className="text-slate-200 font-semibold">{formatPct(stratReport.cagr_pct)}</span>
          </div>

          <div className="flex justify-between py-1">
            <span className="text-slate-400">Expectancy:</span>
            <span className="text-slate-200 font-semibold">
              {formatINR(stratReport.expectancy_rupees)} / trade
            </span>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className="space-y-4">
      {/* Top Header & Controls */}
      <div className="px-5 py-4 rounded-xl bg-[#0e1422] border border-white/10 flex flex-wrap items-center justify-between gap-4 shadow-xl">
        <div>
          <div className="flex items-center gap-2">
            <BarChart3 className="w-5 h-5 text-indigo-400" />
            <h3 className="text-base font-bold text-white tracking-wide">
              Strategy Execution & Performance
            </h3>
          </div>
          <p className="text-xs text-slate-400 mt-0.5">
            Event-driven 180-day backtest comparison on real historical candles (Zero Look-Ahead & Real Costs)
          </p>
        </div>

        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1.5 text-xs font-mono">
            <span className="text-slate-400">Duration:</span>
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              className="bg-black/50 border border-white/10 text-white rounded px-2.5 py-1 text-xs font-mono"
            >
              <option value={90}>90 Days</option>
              <option value={180}>180 Days</option>
              <option value={365}>365 Days</option>
            </select>
          </div>

          <button
            onClick={() => onRunBacktest && onRunBacktest(days, selectedSymbol)}
            disabled={isLoading}
            className="px-4 py-1.5 rounded-lg text-xs font-mono font-bold bg-indigo-600 hover:bg-indigo-500 text-white transition-all flex items-center gap-2 shadow-lg shadow-indigo-600/20 disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${isLoading ? 'animate-spin' : ''}`} />
            <span>{isLoading ? 'Running...' : 'Run Backtest'}</span>
          </button>
        </div>
      </div>

      {/* Strategy 1, 2, 3 Cards Grid */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {/* Strategy 1: ORB */}
        {renderStrategyCard(
          'STRATEGY 1: ORB Breakout',
          '30-Minute Volatility-Filtered Opening Range',
          orb,
          'orb'
        )}

        {/* Strategy 2: CPR */}
        {renderStrategyCard(
          'STRATEGY 2: CPR Regime',
          'Central Pivot Range Breakout & Mean-Reversion',
          cpr,
          'cpr'
        )}

        {/* Strategy 3: Dual-EMA */}
        {renderStrategyCard(
          'STRATEGY 3: Dual-EMA Trend',
          'Adaptive Volatility-Buffered EMA9/21 + SMA200',
          dual,
          'dual_ema'
        )}
      </div>

      {/* Comparative Strategy Summary Table */}
      <div className="rounded-xl bg-[#0e1422] border border-white/10 shadow-xl overflow-hidden">
        <div className="px-5 py-3.5 bg-white/[0.02] border-b border-white/10 flex items-center justify-between">
          <h4 className="text-sm font-bold text-white font-mono uppercase tracking-wider">
            Comparative Strategy Summary
          </h4>
          <span className="text-xs text-slate-400 font-mono">
            {backtestData ? `Instrument: ${selectedSymbol} | ${days} Days` : 'No backtest loaded'}
          </span>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse text-xs font-mono">
            <thead>
              <tr className="border-b border-white/[0.06] bg-white/[0.01] text-[11px] uppercase text-slate-400 tracking-wider">
                <th className="py-3 px-4">Strategy</th>
                <th className="py-3 px-4 text-center">Trades</th>
                <th className="py-3 px-4 text-center">Win Rate</th>
                <th className="py-3 px-4 text-center">Profit Factor</th>
                <th className="py-3 px-4 text-center">Sharpe</th>
                <th className="py-3 px-4 text-center">Max Drawdown</th>
                <th className="py-3 px-4 text-right">Net P&L</th>
                <th className="py-3 px-4 text-center">State</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/[0.04]">
              {comparison.length === 0 ? (
                <tr>
                  <td colSpan={8} className="py-8 text-center text-slate-500">
                    Run backtest to view comparative rankings
                  </td>
                </tr>
              ) : (
                comparison.map((comp) => {
                  const isPos = comp.net_pnl > 0;
                  return (
                    <tr key={comp.strategy_id} className="hover:bg-white/[0.02] transition-colors">
                      <td className="py-3 px-4 font-bold text-white">{comp.strategy}</td>
                      <td className="py-3 px-4 text-center text-slate-300">{comp.trades}</td>
                      <td className="py-3 px-4 text-center text-slate-200">
                        {comp.win_rate?.toFixed(1)}%
                      </td>
                      <td
                        className={`py-3 px-4 text-center font-bold ${
                          comp.profit_factor >= 1.25
                            ? 'text-emerald-400'
                            : comp.profit_factor >= 1.0
                            ? 'text-amber-400'
                            : 'text-rose-400'
                        }`}
                      >
                        {comp.profit_factor?.toFixed(2)}
                      </td>
                      <td className="py-3 px-4 text-center text-indigo-300">
                        {comp.sharpe?.toFixed(2)}
                      </td>
                      <td className="py-3 px-4 text-center text-rose-400">
                        {comp.max_drawdown?.toFixed(1)}%
                      </td>
                      <td
                        className={`py-3 px-4 text-right font-bold ${
                          isPos ? 'text-emerald-400' : 'text-rose-400'
                        }`}
                      >
                        {formatINR(comp.net_pnl)}
                      </td>
                      <td className="py-3 px-4 text-center">
                        <span
                          className={`px-2 py-0.5 rounded text-[11px] font-bold ${
                            comp.state === 'ACTIVE'
                              ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30'
                              : 'bg-slate-800 text-slate-400 border border-slate-700/40'
                          }`}
                        >
                          {comp.state}
                        </span>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
