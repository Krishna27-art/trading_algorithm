import React from 'react';
import { Shield, ShieldAlert, CheckCircle2, Lock, AlertTriangle, Wallet } from 'lucide-react';

export default function RiskPage({ telemetry }) {
  const risk = telemetry?.risk_summary || {};
  const capital = risk.capital || 1000000;
  const limit = risk.daily_risk_limit || (capital * 0.02);
  const used = risk.daily_risk_used || 0;
  const remaining = Math.max(0, limit - used);
  const usedPct = Math.min(100, (used / limit) * 100);
  const tradesTaken = risk.trades_taken || 0;
  const maxTrades = risk.max_trades || 1;

  const formatINR = (val) => {
    return new Intl.NumberFormat('en-IN', {
      style: 'currency',
      currency: 'INR',
      maximumFractionDigits: 0,
    }).format(val || 0);
  };

  return (
    <div className="space-y-4">
      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-white/[0.08]">
        <div>
          <h2 className="text-xl font-extrabold text-white tracking-tight flex items-center gap-2.5">
            <Shield className="w-5 h-5 text-emerald-400" /> Capital Preservation & Risk Controls
          </h2>
          <p className="text-xs text-slate-400 font-mono mt-0.5">
            Automated mathematical gates guarding trading capital against outsized drawdowns
          </p>
        </div>

        <span className="term-badge term-badge-emerald text-xs font-mono font-bold">
          2% HARD KILL SWITCH ACTIVE
        </span>
      </div>

      {/* Metrics Row */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 font-mono">
        <div className="term-panel p-4">
          <span className="text-xs text-slate-400 font-sans uppercase font-semibold">Total Capital</span>
          <span className="text-2xl font-black text-white mt-1 block">{formatINR(capital)}</span>
          <span className="text-xs text-slate-400 font-sans mt-0.5 block">Portfolio Base</span>
        </div>

        <div className="term-panel p-4">
          <span className="text-xs text-slate-400 font-sans uppercase font-semibold">Risk Per Trade</span>
          <span className="text-2xl font-black text-indigo-300 mt-1 block">1.0%</span>
          <span className="text-xs text-slate-400 font-sans mt-0.5 block">{formatINR(capital * 0.01)} / trade</span>
        </div>

        <div className="term-panel p-4">
          <span className="text-xs text-slate-400 font-sans uppercase font-semibold">Daily Loss Limit</span>
          <span className="text-2xl font-black text-rose-400 mt-1 block">{formatINR(limit)}</span>
          <span className="text-xs text-slate-400 font-sans mt-0.5 block">2.0% Circuit Breaker</span>
        </div>

        <div className="term-panel p-4">
          <span className="text-xs text-slate-400 font-sans uppercase font-semibold">Remaining Daily Risk</span>
          <span className="text-2xl font-black text-emerald-400 mt-1 block">{formatINR(remaining)}</span>
          <span className="text-xs text-slate-400 font-sans mt-0.5 block">{formatINR(used)} consumed</span>
        </div>
      </div>

      {/* Visual Risk Gauge */}
      <div className="term-panel p-5 space-y-3">
        <div className="flex justify-between text-xs font-mono text-slate-400">
          <span className="font-sans font-bold text-white uppercase text-sm">Daily Capital Risk Burn Meter</span>
          <span>{usedPct.toFixed(1)}% of 2% Limit Used</span>
        </div>

        <div className="w-full h-4 bg-slate-900 rounded-full overflow-hidden border border-white/10 p-0.5">
          <div
            className={`h-full rounded-full transition-all duration-300 ${
              usedPct > 75 ? 'bg-rose-500' : usedPct > 40 ? 'bg-amber-500' : 'bg-emerald-500'
            }`}
            style={{ width: `${Math.max(3, usedPct)}%` }}
          />
        </div>
      </div>

      {/* 4 Invariant Safeguard Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div className="term-panel p-4 space-y-2">
          <div className="flex items-center gap-2">
            <CheckCircle2 className="w-5 h-5 text-emerald-400" />
            <h3 className="text-sm font-bold text-white">Dynamic Position Sizing</h3>
          </div>
          <p className="text-xs text-slate-300 leading-relaxed font-sans">
            Contracts are sized dynamically based on the exact Stop Loss distance so that if SL is triggered, the loss never exceeds 1% of equity. If OR Width &gt; 120 pts, distance is clamped at 80 pts.
          </p>
        </div>

        <div className="term-panel p-4 space-y-2">
          <div className="flex items-center gap-2">
            <Lock className="w-5 h-5 text-indigo-400" />
            <h3 className="text-sm font-bold text-white">1 Trade / Instrument / Day</h3>
          </div>
          <p className="text-xs text-slate-300 leading-relaxed font-sans">
            Once a position is entered and concluded for NIFTY, the algorithm locks and takes zero further trades for the rest of the day, eliminating revenge trading and churn.
          </p>
        </div>

        <div className="term-panel p-4 space-y-2">
          <div className="flex items-center gap-2">
            <ShieldAlert className="w-5 h-5 text-rose-400" />
            <h3 className="text-sm font-bold text-white">2% Daily Circuit Breaker</h3>
          </div>
          <p className="text-xs text-slate-300 leading-relaxed font-sans">
            If total daily realized + unrealized drawdown touches 2% of capital, the system immediately squares off open MIS positions and revokes order authorization until next morning.
          </p>
        </div>

        <div className="term-panel p-4 space-y-2">
          <div className="flex items-center gap-2">
            <CheckCircle2 className="w-5 h-5 text-cyan-400" />
            <h3 className="text-sm font-bold text-white">Zero Overnight Exposure</h3>
          </div>
          <p className="text-xs text-slate-300 leading-relaxed font-sans">
            All positions are strictly intraday MIS contracts. Mandatory square-off executes at 14:30 IST, and a hard cutoff applies at 15:10 IST, insulating against gap-open risk.
          </p>
        </div>
      </div>
    </div>
  );
}
