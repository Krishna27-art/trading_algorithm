import React from 'react';
import { Clock, ShieldAlert, CheckCircle2, ArrowUpRight, ArrowDownRight } from 'lucide-react';

export default function ActiveTradeCard({ trade, onSquareOff }) {
  if (!trade) {
    return (
      <div className="term-panel p-4 space-y-3 flex flex-col justify-between">
        <div className="flex items-center justify-between pb-2.5 border-b border-white/[0.08]">
          <h3 className="text-sm font-bold text-white uppercase tracking-wider">Active Position</h3>
          <span className="term-badge term-badge-neutral text-xs font-mono">FLAT (0)</span>
        </div>
        <div className="py-6 text-center text-sm text-slate-400 font-mono">
          NO ACTIVE TRADE
          <span className="text-xs text-slate-400 block mt-1">Capital is fully protected. Zero market exposure.</span>
        </div>
      </div>
    );
  }

  const isLong = trade.direction === 'BUY';
  const entry = trade.entry_price || 24100.0;
  const ltp = trade.current_price || entry;
  const sl = trade.stop_loss || 24000.0;
  const target = trade.target || 24300.0;
  const pnl = trade.unrealized_pnl || 0.0;
  const rMult = trade.r_multiple || 0.0;

  // Calculate percentage progress toward target vs SL
  const totalSpan = Math.abs(target - sl) || 1;
  const currentSpan = isLong ? (ltp - sl) : (sl - ltp);
  const progressPct = Math.max(0, Math.min(100, (currentSpan / totalSpan) * 100));

  const formatINR = (val) => {
    const num = Math.round(val || 0);
    return (num >= 0 ? '+' : '') + `₹${num.toLocaleString('en-IN')}`;
  };

  const tradeStatus = trade.status || (rMult >= 1.0 ? 'BREAKEVEN' : 'ACTIVE');

  return (
    <div className="term-panel p-4 space-y-3.5 border-2 border-indigo-500/30">
      {/* Header */}
      <div className="flex items-center justify-between pb-2.5 border-b border-white/[0.08]">
        <div className="flex items-center gap-2.5">
          <span className={`term-badge ${isLong ? 'term-badge-emerald' : 'term-badge-rose'} text-xs font-bold`}>
            {trade.direction}
          </span>
          <span className="text-sm sm:text-base font-bold text-white font-mono">{trade.symbol}</span>
          <span className="text-xs text-slate-400 font-mono">({trade.quantity} Qty)</span>
          <span className="term-badge term-badge-cyan text-xs font-mono font-bold">
            {tradeStatus}
          </span>
        </div>

        <div className="flex items-center gap-2">
          <span className="text-xs text-slate-400 flex items-center gap-1 font-mono">
            <Clock className="w-3.5 h-3.5" /> {trade.duration_mins || 12}m
          </span>
          <button
            onClick={onSquareOff}
            className="text-xs font-mono font-bold px-3 py-1 rounded-md bg-rose-500/10 text-rose-300 border border-rose-500/30 hover:bg-rose-500/20 transition-colors"
          >
            EXIT NOW
          </button>
        </div>
      </div>

      {/* Large P&L & R-Multiple Display */}
      <div className="flex items-baseline justify-between p-3.5 rounded-lg bg-black/40 border border-white/5 font-mono">
        <div>
          <span className="text-xs text-slate-400 font-sans block uppercase font-semibold">Unrealized P&L</span>
          <span className={`text-2xl sm:text-3xl font-extrabold tracking-tight ${pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
            {formatINR(pnl)}
          </span>
        </div>

        <div className="text-right">
          <span className="text-xs text-slate-400 font-sans block uppercase font-semibold">R-Multiple</span>
          <span className={`text-xl sm:text-2xl font-bold ${rMult >= 1.0 ? 'text-emerald-400' : 'text-slate-200'}`}>
            +{rMult.toFixed(2)}R
          </span>
        </div>
      </div>

      {/* Grid of Trade Parameters */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 font-mono text-xs">
        <div className="p-2 rounded bg-white/[0.02] border border-white/5">
          <span className="text-[10px] text-slate-400 block uppercase">Entry</span>
          <span className="font-bold text-white text-sm">₹{entry.toFixed(1)}</span>
        </div>
        <div className="p-2 rounded bg-white/[0.02] border border-white/5">
          <span className="text-[10px] text-slate-400 block uppercase">Current LTP</span>
          <span className="font-bold text-cyan-300 text-sm">₹{ltp.toFixed(1)}</span>
        </div>
        <div className="p-2 rounded bg-rose-500/[0.04] border border-rose-500/20">
          <span className="text-[10px] text-slate-400 block uppercase">Stop Loss</span>
          <span className="font-bold text-rose-400 text-sm">₹{sl.toFixed(1)}</span>
        </div>
        <div className="p-2 rounded bg-emerald-500/[0.04] border border-emerald-500/20">
          <span className="text-[10px] text-slate-400 block uppercase">Target</span>
          <span className="font-bold text-emerald-400 text-sm">₹{target.toFixed(1)}</span>
        </div>
      </div>

      {/* Visual SL ───────── ENTRY ───────── TARGET Progress Bar */}
      <div className="space-y-2 pt-1 font-mono text-xs">
        <div className="flex justify-between text-slate-400 font-semibold">
          <span className="text-rose-400">SL: ₹{sl.toFixed(0)}</span>
          <span className="text-slate-200">ENTRY: ₹{entry.toFixed(0)}</span>
          <span className="text-emerald-400">TARGET: ₹{target.toFixed(0)}</span>
        </div>

        <div className="w-full h-3 bg-slate-900 rounded-full overflow-hidden p-0.5 border border-white/10 relative">
          {/* Entry marker */}
          <div className="absolute top-0 bottom-0 left-1/2 w-0.5 bg-white/60 z-10" title="Entry Price" />
          {/* Progress fill */}
          <div
            className={`h-full rounded-full transition-all duration-300 ${
              pnl >= 0 ? 'bg-gradient-to-r from-indigo-500 to-emerald-400' : 'bg-gradient-to-r from-rose-500 to-amber-500'
            }`}
            style={{ width: `${progressPct}%` }}
          />
        </div>

        <div className="flex justify-between text-[11px] text-slate-400">
          <span>Initial Risk (-1R)</span>
          <span className="text-cyan-300 font-semibold">Live LTP ₹{ltp.toFixed(1)}</span>
          <span>Target Profit (+2R)</span>
        </div>
      </div>
    </div>
  );
}
