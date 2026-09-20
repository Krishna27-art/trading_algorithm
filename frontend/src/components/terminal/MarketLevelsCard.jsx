import React from 'react';
import { Target, Info, CheckCircle2, AlertCircle } from 'lucide-react';

export default function MarketLevelsCard({ telemetry }) {
  const orbHigh = telemetry?.orb_high || 24132.0;
  const orbLow = telemetry?.orb_low || 24031.0;
  const orbWidth = telemetry?.orb_width || (orbHigh - orbLow);
  const vwap = telemetry?.vwap || 24095.0;
  const ltp = telemetry?.current_price || 23980.0;
  const volFilterPassed = telemetry?.volatility_filter_passed ?? true;

  return (
    <div className="term-panel p-4 space-y-3">
      {/* Header */}
      <div className="flex items-center justify-between pb-2.5 border-b border-white/[0.08]">
        <div className="flex items-center gap-2">
          <Target className="w-4 h-4 text-cyan-400" />
          <h3 className="text-sm font-bold text-white uppercase tracking-wider">
            Market Levels & Microstructure
          </h3>
        </div>
        <span className="term-badge term-badge-cyan text-xs font-mono">
          NIFTY INTRADAY
        </span>
      </div>

      {/* Grid of 5 Key Market Levels */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 font-mono">
        {/* 1. OR High */}
        <div className="p-3 rounded-lg bg-emerald-500/[0.04] border border-emerald-500/20 flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400 font-sans font-semibold uppercase">OR High</span>
            <span className="text-[10px] text-emerald-400 bg-emerald-500/10 px-1 rounded">09:45</span>
          </div>
          <div className="mt-2">
            <span className="text-xl sm:text-2xl font-bold text-emerald-400">
              ₹{orbHigh.toFixed(1)}
            </span>
            <span className="text-[11px] text-slate-400 block font-sans mt-0.5" title="Highest price during 09:15–09:45">
              Breakout Barrier
            </span>
          </div>
        </div>

        {/* 2. OR Low */}
        <div className="p-3 rounded-lg bg-rose-500/[0.04] border border-rose-500/20 flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400 font-sans font-semibold uppercase">OR Low</span>
            <span className="text-[10px] text-rose-400 bg-rose-500/10 px-1 rounded">09:45</span>
          </div>
          <div className="mt-2">
            <span className="text-xl sm:text-2xl font-bold text-rose-400">
              ₹{orbLow.toFixed(1)}
            </span>
            <span className="text-[11px] text-slate-400 block font-sans mt-0.5" title="Lowest price during 09:15–09:45">
              Breakdown Barrier
            </span>
          </div>
        </div>

        {/* 3. OR Width */}
        <div className="p-3 rounded-lg bg-indigo-500/[0.04] border border-indigo-500/20 flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400 font-sans font-semibold uppercase">OR Width</span>
            {volFilterPassed ? (
              <span className="text-[10px] text-emerald-400 bg-emerald-500/10 px-1 rounded font-bold">≥40 PASS</span>
            ) : (
              <span className="text-[10px] text-amber-400 bg-amber-500/10 px-1 rounded font-bold">&lt;40 FAIL</span>
            )}
          </div>
          <div className="mt-2">
            <span className="text-xl sm:text-2xl font-bold text-indigo-300">
              {orbWidth.toFixed(1)} <span className="text-sm font-normal text-slate-400">pts</span>
            </span>
            <span className="text-[11px] text-slate-400 block font-sans mt-0.5">
              Volatility Span
            </span>
          </div>
        </div>

        {/* 4. Session VWAP */}
        <div className="p-3 rounded-lg bg-cyan-500/[0.04] border border-cyan-500/20 flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400 font-sans font-semibold uppercase">Session VWAP</span>
            <span className="text-[10px] text-cyan-300 bg-cyan-500/10 px-1 rounded">Anchor</span>
          </div>
          <div className="mt-2">
            <span className="text-xl sm:text-2xl font-bold text-cyan-300">
              ₹{vwap.toFixed(1)}
            </span>
            <span className="text-[11px] text-slate-400 block font-sans mt-0.5" title="Volume Weighted Average Price">
              Trend Baseline
            </span>
          </div>
        </div>

        {/* 5. Live Price (LTP) */}
        <div className="p-3 rounded-lg bg-white/[0.03] border border-white/10 flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400 font-sans font-semibold uppercase">Current LTP</span>
            <span className="text-[10px] text-slate-400 bg-white/5 px-1 rounded">Live</span>
          </div>
          <div className="mt-2">
            <span className="text-xl sm:text-2xl font-bold text-white">
              ₹{ltp.toFixed(1)}
            </span>
            <span className="text-[11px] text-slate-400 block font-sans mt-0.5">
              Last Traded Price
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
