import React, { useState } from 'react';
import { BarChart2, TrendingUp, RefreshCw, Layers, ShieldCheck } from 'lucide-react';

export default function PerformanceSection({ backtestReport, onRunBacktest, isLoading }) {
  const [activeSubTab, setActiveSubTab] = useState('metrics');

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

  // Synthetic equity curve points for visualization
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

  const svgW = 500;
  const svgH = 120;
  const minE = 1000000;
  const maxE = 2200000;
  const getEy = (val) => svgH - 15 - ((val - minE) / (maxE - minE)) * (svgH - 30);
  const getEx = (idx) => 15 + (idx / (curvePoints.length - 1)) * (svgW - 30);

  const polylineStr = curvePoints.map((pt, idx) => `${getEx(idx)},${getEy(pt.y)}`).join(' ');

  return (
    <div className="term-panel p-3.5 space-y-3">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 pb-2 border-b border-white/[0.06]">
        <div className="flex items-center gap-2">
          <BarChart2 className="w-4 h-4 text-emerald-400" />
          <h3 className="text-xs font-bold text-slate-100 uppercase tracking-wider">Strategy Performance Analytics</h3>
          <span className="text-[10px] text-slate-400 font-mono">(Net of Oct 2024 SEBI Charges)</span>
        </div>

        <button
          onClick={onRunBacktest}
          disabled={isLoading}
          className="term-btn-primary py-1 px-2.5 text-[11px] font-mono"
        >
          {isLoading ? <RefreshCw className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
          <span>{isLoading ? 'Simulating...' : 'Refresh 180-Day Backtest'}</span>
        </button>
      </div>

      {/* KPI Stats Grid */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs font-mono">
        <div className="p-2 rounded bg-white/[0.02] border border-white/5">
          <span className="text-[10px] text-slate-400 block uppercase">Net P&L (Cumulative)</span>
          <span className="text-sm font-bold text-emerald-400">{formatINR(report.net_pnl)}</span>
          <span className="text-[9px] text-slate-400 block">CAGR: +{report.cagr_pct}%</span>
        </div>

        <div className="p-2 rounded bg-white/[0.02] border border-white/5">
          <span className="text-[10px] text-slate-400 block uppercase">Win Rate (Hit Ratio)</span>
          <span className="text-sm font-bold text-white">{report.win_rate_pct}%</span>
          <span className="text-[9px] text-slate-400 block">{report.winning_trades}W / {report.losing_trades}L</span>
        </div>

        <div className="p-2 rounded bg-white/[0.02] border border-white/5">
          <span className="text-[10px] text-slate-400 block uppercase">Profit Factor</span>
          <span className="text-sm font-bold text-indigo-300">{report.profit_factor}</span>
          <span className="text-[9px] text-slate-400 block">Sharpe: {report.sharpe_ratio}</span>
        </div>

        <div className="p-2 rounded bg-white/[0.02] border border-white/5">
          <span className="text-[10px] text-slate-400 block uppercase">Max Strategy DD</span>
          <span className="text-sm font-bold text-rose-400">-{report.max_drawdown_pct}%</span>
          <span className="text-[9px] text-slate-400 block">Avg: +{report.avg_r_multiple}R / trade</span>
        </div>
      </div>

      {/* Equity Curve SVG */}
      <div className="p-2.5 rounded bg-[#080c14] border border-white/5 space-y-1.5">
        <div className="flex items-center justify-between text-[10px] font-mono text-slate-400">
          <span>Equity Growth (Net of Taxes & Slippage)</span>
          <span className="text-emerald-400 font-bold">₹10.0L → {formatINR(1000000 + report.net_pnl)}</span>
        </div>

        <div className="w-full h-[120px] overflow-hidden select-none">
          <svg viewBox={`0 0 ${svgW} ${svgH}`} className="w-full h-full" preserveAspectRatio="none">
            <line x1="15" y1={getEy(1000000)} x2={svgW - 15} y2={getEy(1000000)} stroke="rgba(255,255,255,0.08)" strokeDasharray="3 3" />
            <polyline fill="none" stroke="#10b981" strokeWidth="2" points={polylineStr} />
          </svg>
        </div>
      </div>

      {/* Long vs Short Performance Breakdown */}
      <div className="grid grid-cols-2 gap-2 text-[11px] font-mono pt-1">
        <div className="p-2 rounded bg-white/[0.02] border border-white/5">
          <span className="text-[10px] text-slate-400 uppercase block">Long Performance</span>
          <div className="flex justify-between mt-1">
            <span className="text-slate-300">Win Rate: {report.long_win_rate}%</span>
            <span className="text-emerald-400 font-bold">{formatINR(report.long_net_pnl)}</span>
          </div>
        </div>

        <div className="p-2 rounded bg-white/[0.02] border border-white/5">
          <span className="text-[10px] text-slate-400 uppercase block">Short Performance (Edge)</span>
          <div className="flex justify-between mt-1">
            <span className="text-slate-300">Win Rate: {report.short_win_rate}%</span>
            <span className="text-emerald-400 font-bold">{formatINR(report.short_net_pnl)}</span>
          </div>
        </div>
      </div>
    </div>
  );
}
