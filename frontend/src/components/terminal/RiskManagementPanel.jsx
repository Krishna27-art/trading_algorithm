import React from 'react';
import { Shield, ShieldAlert, AlertTriangle, CheckCircle2, Lock } from 'lucide-react';

export default function RiskManagementPanel({ telemetry }) {
  const risk = telemetry?.risk_summary || {};
  const capital = risk.capital || 1000000;
  const limit = risk.daily_risk_limit || (capital * 0.02);
  const used = risk.daily_risk_used || 0;
  const remaining = Math.max(0, limit - used);
  const usedPct = Math.min(100, (used / limit) * 100);

  const tradesTaken = risk.trades_taken || 0;
  const maxTrades = risk.max_trades || 1;
  const tradesRemaining = Math.max(0, maxTrades - tradesTaken);

  const isHalted = used >= limit || tradesTaken >= maxTrades;

  const formatINR = (val) => {
    return new Intl.NumberFormat('en-IN', {
      style: 'currency',
      currency: 'INR',
      maximumFractionDigits: 0,
    }).format(val || 0);
  };

  return (
    <div className="term-panel p-4 space-y-3.5">
      {/* Header */}
      <div className="flex items-center justify-between pb-2.5 border-b border-white/[0.08]">
        <div className="flex items-center gap-2">
          <Shield className="w-4 h-4 text-emerald-400" />
          <h3 className="text-sm font-bold text-white uppercase tracking-wider">Daily Risk & Capital Guard</h3>
        </div>
        {isHalted ? (
          <span className="term-badge term-badge-rose text-xs font-mono font-bold">
            TRADING HALTED
          </span>
        ) : (
          <span className="term-badge term-badge-emerald text-xs font-mono font-bold">
            ARMED & ACTIVE
          </span>
        )}
      </div>

      {/* Trading Halted Warning if Circuit Breaker active */}
      {isHalted && (
        <div className="p-3 rounded-lg bg-rose-500/10 border border-rose-500/30 flex items-center gap-2.5 text-xs text-rose-300 font-bold">
          <ShieldAlert className="w-5 h-5 text-rose-400 shrink-0" />
          <span>TRADING HALTED: Daily threshold or 1 Trade/Day execution cap reached.</span>
        </div>
      )}

      {/* Daily Loss Metric Row */}
      <div className="space-y-2 font-mono">
        <div className="flex justify-between items-baseline">
          <span className="text-xs text-slate-400 font-sans font-semibold uppercase">Daily Loss Limit (2.0%)</span>
          <span className="text-base font-bold text-white">{formatINR(limit)}</span>
        </div>

        {/* Visual Progress Bar */}
        <div className="w-full h-3 bg-slate-900 rounded-full overflow-hidden border border-white/10 p-0.5">
          <div
            className={`h-full rounded-full transition-all duration-300 ${
              usedPct > 75 ? 'bg-rose-500' : usedPct > 40 ? 'bg-amber-500' : 'bg-emerald-500'
            }`}
            style={{ width: `${Math.max(4, usedPct)}%` }}
          />
        </div>

        <div className="flex justify-between text-xs text-slate-300 font-semibold">
          <span>Used: <strong className="text-rose-400">{formatINR(used)}</strong></span>
          <span>Remaining: <strong className="text-emerald-400">{formatINR(remaining)}</strong></span>
        </div>
      </div>

      {/* Risk Parameters Grid */}
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-2.5 font-mono text-xs pt-1">
        <div className="p-2.5 rounded-lg bg-white/[0.02] border border-white/5">
          <span className="text-[11px] text-slate-400 font-sans block uppercase font-semibold">Risk Per Trade</span>
          <span className="text-sm font-bold text-white mt-1 block">1.0%</span>
          <span className="text-[10px] text-slate-400 font-sans mt-0.5 block">{formatINR(capital * 0.01)}</span>
        </div>

        <div className="p-2.5 rounded-lg bg-white/[0.02] border border-white/5">
          <span className="text-[11px] text-slate-400 font-sans block uppercase font-semibold">Trades Today</span>
          <span className="text-sm font-bold text-white mt-1 block">{tradesTaken} / {maxTrades}</span>
          <span className="text-[10px] text-slate-400 font-sans mt-0.5 block">{tradesRemaining} Left</span>
        </div>

        <div className="p-2.5 rounded-lg bg-white/[0.02] border border-white/5 col-span-2 sm:col-span-1">
          <span className="text-[11px] text-slate-400 font-sans block uppercase font-semibold">Daily Kill Switch</span>
          <span className="text-sm font-bold text-rose-400 mt-1 block">2.0%</span>
          <span className="text-[10px] text-slate-400 font-sans mt-0.5 block">{formatINR(limit)}</span>
        </div>
      </div>
    </div>
  );
}
