import React from 'react';
import { ArrowUpRight, ArrowDownRight, Zap, CheckCircle2, ShieldAlert } from 'lucide-react';

export default function SignalCard({ signal, onQuickExecute, isLiveMode }) {
  const formatINR = (val) => `₹${Math.round(val || 0).toLocaleString('en-IN')}`;

  if (!signal) {
    return (
      <div className="term-panel p-4 space-y-3 flex flex-col justify-between">
        {/* Header */}
        <div className="flex items-center justify-between pb-2.5 border-b border-white/[0.08]">
          <div className="flex items-center gap-2">
            <Zap className="w-4 h-4 text-slate-400" />
            <h3 className="text-sm font-bold text-white uppercase tracking-wider">Algorithmic Signal</h3>
          </div>
          <span className="term-badge term-badge-neutral text-xs font-mono">SCANNING</span>
        </div>

        {/* Large Prominent Status */}
        <div className="py-6 text-center space-y-2">
          <span className="text-2xl sm:text-3xl font-black text-slate-300 tracking-tight block">
            NO ACTIVE SIGNAL
          </span>
          <p className="text-xs sm:text-sm text-slate-400 max-w-sm mx-auto leading-relaxed">
            Waiting for price confirmation. Monitoring completed 15m candle close beyond OR boundaries with VWAP alignment.
          </p>
        </div>

        {/* Real-time Checklist */}
        <div className="space-y-1.5 p-3 rounded-lg bg-black/40 border border-white/5 text-xs font-mono">
          <div className="flex items-center justify-between text-slate-300">
            <span>1. 15m Close &gt; OR High / &lt; OR Low</span>
            <span className="text-amber-400 font-bold">Scanning</span>
          </div>
          <div className="flex items-center justify-between text-slate-300">
            <span>2. Session VWAP Confirmation</span>
            <span className="text-amber-400 font-bold">Monitoring</span>
          </div>
          <div className="flex items-center justify-between text-slate-300">
            <span>3. Volatility Filter (Width &ge; 40 pts)</span>
            <span className="text-emerald-400 font-bold">PASSED</span>
          </div>
        </div>
      </div>
    );
  }

  const isLong = signal.type === 'LONG';
  const qty = signal.quantity || 25;

  return (
    <div className={`term-panel p-4 space-y-3.5 border-2 ${
      isLong
        ? 'border-emerald-500/40 bg-emerald-500/[0.03]'
        : 'border-rose-500/40 bg-rose-500/[0.03]'
    }`}>
      {/* Header */}
      <div className="flex items-center justify-between pb-2.5 border-b border-white/[0.08]">
        <div className="flex items-center gap-2">
          <div className={`p-1.5 rounded-md ${isLong ? 'bg-emerald-500/20 text-emerald-400' : 'bg-rose-500/20 text-rose-400'}`}>
            {isLong ? <ArrowUpRight className="w-5 h-5" /> : <ArrowDownRight className="w-5 h-5" />}
          </div>
          <span className="text-xs font-mono font-bold text-slate-300 uppercase">
            {signal.symbol || 'NIFTY 50'}
          </span>
        </div>

        <span className={`term-badge ${isLong ? 'term-badge-emerald' : 'term-badge-rose'} text-xs font-mono font-bold`}>
          {signal.confidence || 85}% Confidence
        </span>
      </div>

      {/* Prominent Large Signal Title (26-30px) */}
      <div>
        <span className={`text-2xl sm:text-3xl font-black tracking-tight block ${
          isLong ? 'text-emerald-400' : 'text-rose-400'
        }`}>
          {signal.type} SIGNAL
        </span>
        <p className="text-xs text-slate-300 mt-1 font-sans">
          <strong className="text-white">Reason: </strong>
          {signal.trigger || (isLong ? '15m close above OR High and above VWAP' : '15m close below OR Low and below VWAP')}
        </p>
      </div>

      {/* Numerical Levels Grid (20-24px) */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5 font-mono">
        <div className="p-2.5 rounded-lg bg-white/[0.03] border border-white/5">
          <span className="text-[11px] text-slate-400 block uppercase font-sans font-semibold">Entry</span>
          <span className="text-lg sm:text-xl font-bold text-white mt-0.5 block">
            ₹{signal.entry?.toFixed(1)}
          </span>
        </div>

        <div className="p-2.5 rounded-lg bg-rose-500/[0.05] border border-rose-500/20">
          <span className="text-[11px] text-slate-400 block uppercase font-sans font-semibold">Stop Loss</span>
          <span className="text-lg sm:text-xl font-bold text-rose-400 mt-0.5 block">
            ₹{signal.stop_loss?.toFixed(1)}
          </span>
        </div>

        <div className="p-2.5 rounded-lg bg-emerald-500/[0.05] border border-emerald-500/20">
          <span className="text-[11px] text-slate-400 block uppercase font-sans font-semibold">Target (2R)</span>
          <span className="text-lg sm:text-xl font-bold text-emerald-400 mt-0.5 block">
            ₹{signal.target?.toFixed(1)}
          </span>
        </div>

        <div className="p-2.5 rounded-lg bg-indigo-500/[0.05] border border-indigo-500/20">
          <span className="text-[11px] text-slate-400 block uppercase font-sans font-semibold">Quantity</span>
          <span className="text-lg sm:text-xl font-bold text-indigo-300 mt-0.5 block">
            {qty} <span className="text-xs font-normal text-slate-400">Qty</span>
          </span>
        </div>
      </div>

      {/* Risk / Reward Metrics Row */}
      <div className="flex items-center justify-between p-3 rounded-lg bg-black/40 border border-white/5 text-xs font-mono">
        <div>
          <span className="text-slate-400 block text-[11px] font-sans">Risk (1% Cap)</span>
          <span className="text-sm font-bold text-rose-400">{formatINR(signal.risk_amount || 10000)}</span>
        </div>
        <div className="h-6 w-[1px] bg-white/10" />
        <div>
          <span className="text-slate-400 block text-[11px] font-sans">Reward (2R)</span>
          <span className="text-sm font-bold text-emerald-400">{formatINR(signal.reward_amount || 20000)}</span>
        </div>
        <div className="h-6 w-[1px] bg-white/10" />
        <div>
          <span className="text-slate-400 block text-[11px] font-sans">Risk / Reward</span>
          <span className="text-sm font-bold text-indigo-300">{signal.risk_reward || '1 : 2.0'}</span>
        </div>
      </div>

      {/* Action Button */}
      <button
        onClick={() => onQuickExecute(signal)}
        className={`w-full py-3 ${
          isLong ? 'term-btn-emerald' : 'term-btn-rose'
        } text-sm font-bold font-mono tracking-wider shadow-lg`}
      >
        <span>EXECUTE {signal.type} ({isLiveMode ? 'LIVE REAL ORDER' : 'PAPER SIMULATION'})</span>
      </button>
    </div>
  );
}
