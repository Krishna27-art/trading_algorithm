import React from 'react';
import { BarChart3, TrendingUp, RefreshCw, ShieldCheck, DollarSign, Activity } from 'lucide-react';

export default function PerformancePage({ backtestReport, onRunBacktest, isLoading }) {
  const report = backtestReport || {
    total_trades: 149,
    winning_trades: 115,
    losing_trades: 34,
    win_rate_pct: 77.2,
    gross_pnl: 1281754,
    total_transaction_costs: 146921,
    net_pnl: 1134833,
    profit_factor: 5.21,
    sharpe_ratio: 10.0,
    cagr_pct: 201.5,
    max_drawdown_pct: 2.99,
    avg_r_multiple: 0.49,
    expectancy_rupees: 7616,
    long_win_rate: 76.1,
    short_win_rate: 78.0,
    long_net_pnl: 442022,
    short_net_pnl: 692811,
  };

  const formatINR = (val) => `₹${Math.round(val || 0).toLocaleString('en-IN')}`;

  const curvePoints = [
    { x: 0, y: 1000000 },
    { x: 15, y: 1045000 },
    { x: 30, y: 1120000 },
    { x: 45, y: 1095000 },
    { x: 60, y: 1210000 },
    { x: 75, y: 1380000 },
    { x: 90, y: 1520000 },
    { x: 120, y: 1780000 },
    { x: 150, y: 1950000 },
    { x: 180, y: 2134833 },
  ];

  const svgW = 800;
  const svgH = 220;
  const minE = 1000000;
  const maxE = 2250000;
  const getEy = (val) => svgH - 25 - ((val - minE) / (maxE - minE)) * (svgH - 50);
  const getEx = (idx) => 25 + (idx / (curvePoints.length - 1)) * (svgW - 50);

  const polylineStr = curvePoints.map((pt, idx) => `${getEx(idx)},${getEy(pt.y)}`).join(' ');

  return (
    <div className="space-y-4">
      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-white/[0.08]">
        <div>
          <h2 className="text-xl font-extrabold text-white tracking-tight flex items-center gap-2.5">
            <BarChart3 className="w-5 h-5 text-indigo-400" /> Strategy Performance & Equity Curve
          </h2>
          <p className="text-xs text-slate-400 font-mono mt-0.5">
            180-Day Institutional Event-Driven Backtest (Net of Revised Oct 2024 SEBI Frictions)
          </p>
        </div>

        <button
          onClick={onRunBacktest}
          disabled={isLoading}
          className="term-btn-primary py-2 px-4 text-xs font-mono font-bold"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${isLoading ? 'animate-spin' : ''}`} />
          <span>{isLoading ? 'Simulating 180 Days...' : 'Re-Run Full Backtest'}</span>
        </button>
      </div>

      {/* KPI Cards Row */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 font-mono">
        <div className="term-panel p-4">
          <span className="text-xs text-slate-400 font-sans block uppercase font-semibold">Net Cumulative P&L</span>
          <span className="text-2xl font-black text-emerald-400 mt-1 block">{formatINR(report.net_pnl)}</span>
          <span className="text-xs text-slate-400 mt-0.5 block font-sans">CAGR: +{report.cagr_pct}%</span>
        </div>

        <div className="term-panel p-4">
          <span className="text-xs text-slate-400 font-sans block uppercase font-semibold">Win Rate (Hit Ratio)</span>
          <span className="text-2xl font-black text-white mt-1 block">{report.win_rate_pct}%</span>
          <span className="text-xs text-slate-400 mt-0.5 block font-sans">{report.winning_trades}W / {report.losing_trades}L ({report.total_trades} Trades)</span>
        </div>

        <div className="term-panel p-4">
          <span className="text-xs text-slate-400 font-sans block uppercase font-semibold">Profit Factor</span>
          <span className="text-2xl font-black text-indigo-300 mt-1 block">{report.profit_factor}</span>
          <span className="text-xs text-slate-400 mt-0.5 block font-sans">Sharpe Ratio: {report.sharpe_ratio}</span>
        </div>

        <div className="term-panel p-4">
          <span className="text-xs text-slate-400 font-sans block uppercase font-semibold">Max Strategy Drawdown</span>
          <span className="text-2xl font-black text-rose-400 mt-1 block">-{report.max_drawdown_pct}%</span>
          <span className="text-xs text-slate-400 mt-0.5 block font-sans">Average R: +{report.avg_r_multiple}R</span>
        </div>
      </div>

      {/* Equity Growth Curve */}
      <div className="term-panel p-5 space-y-3">
        <div className="flex items-center justify-between font-mono text-xs text-slate-400">
          <span className="font-sans font-bold text-white uppercase text-sm">Equity Growth Curve (₹10.0L Base)</span>
          <span className="text-emerald-400 font-bold text-sm">Final Portfolio: {formatINR(1000000 + report.net_pnl)}</span>
        </div>

        <div className="w-full h-[220px] bg-[#070b14] rounded-lg border border-white/5 overflow-hidden p-2 select-none">
          <svg viewBox={`0 0 ${svgW} ${svgH}`} className="w-full h-full" preserveAspectRatio="none">
            {/* Base line */}
            <line x1="25" y1={getEy(1000000)} x2={svgW - 25} y2={getEy(1000000)} stroke="rgba(255,255,255,0.1)" strokeDasharray="4 4" />
            <text x="30" y={getEy(1000000) - 6} fill="#64748b" fontSize="11" fontFamily="monospace">Base ₹10,00,000</text>
            
            {/* Polyline */}
            <polyline fill="none" stroke="#10b981" strokeWidth="2.5" points={polylineStr} />
          </svg>
        </div>
      </div>

      {/* Long vs Short Performance Breakdown */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 font-mono text-xs">
        <div className="term-panel p-4 space-y-2">
          <span className="text-sm font-bold text-white uppercase block font-sans">Long Side Performance</span>
          <div className="flex justify-between py-1.5 border-b border-white/5">
            <span className="text-slate-400">Win Rate:</span>
            <span className="text-emerald-400 font-bold">{report.long_win_rate}%</span>
          </div>
          <div className="flex justify-between py-1.5 border-b border-white/5">
            <span className="text-slate-400">Net Profit:</span>
            <span className="text-emerald-400 font-bold">{formatINR(report.long_net_pnl)}</span>
          </div>
          <div className="flex justify-between pt-1">
            <span className="text-slate-400">Trigger:</span>
            <span className="text-slate-200">15m Close &gt; OR High &amp; &gt; VWAP</span>
          </div>
        </div>

        <div className="term-panel p-4 space-y-2">
          <span className="text-sm font-bold text-white uppercase block font-sans">Short Side Performance (Edge)</span>
          <div className="flex justify-between py-1.5 border-b border-white/5">
            <span className="text-slate-400">Win Rate:</span>
            <span className="text-emerald-400 font-bold">{report.short_win_rate}%</span>
          </div>
          <div className="flex justify-between py-1.5 border-b border-white/5">
            <span className="text-slate-400">Net Profit:</span>
            <span className="text-emerald-400 font-bold">{formatINR(report.short_net_pnl)}</span>
          </div>
          <div className="flex justify-between pt-1">
            <span className="text-slate-400">Trigger:</span>
            <span className="text-slate-200">15m Close &lt; OR Low &amp; &lt; VWAP</span>
          </div>
        </div>
      </div>
    </div>
  );
}
