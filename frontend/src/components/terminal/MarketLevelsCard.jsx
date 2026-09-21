import React from 'react';
import { Target, Layers, TrendingUp } from 'lucide-react';

export default function MarketLevelsCard({ telemetry }) {
  const strategy = (telemetry?.strategy || 'cpr').toLowerCase();
  const levels = telemetry?.strategy_levels || {};
  const vwap = telemetry?.vwap || 0.0;
  const ltp = telemetry?.current_price || 0.0;
  const symbol = telemetry?.symbol || 'NIFTY';

  const formatPrice = (p) => p ? `₹${Number(p).toFixed(2)}` : '—';

  return (
    <div className="term-panel p-4 space-y-3 font-mono">
      {/* Header */}
      <div className="flex items-center justify-between pb-2.5 border-b border-white/[0.08]">
        <div className="flex items-center gap-2">
          <Target className="w-4 h-4 text-cyan-400" />
          <h3 className="text-sm font-bold text-white uppercase tracking-wider">
            {strategy.toUpperCase()} Key Strategy Levels & Microstructure
          </h3>
        </div>
        <span className="term-badge term-badge-cyan text-xs">
          {symbol} INTRADAY
        </span>
      </div>

      {strategy === 'cpr' && (
        <div className="grid grid-cols-2 sm:grid-cols-6 gap-2.5">
          <div className="p-3 rounded-lg bg-indigo-500/[0.06] border border-indigo-500/20">
            <span className="text-[11px] text-indigo-300 uppercase block font-sans font-semibold">Top Central (TC)</span>
            <span className="text-lg font-bold text-white mt-1 block">{formatPrice(levels.top_central)}</span>
            <span className="text-[10px] text-slate-400 font-sans">Upper Pivot</span>
          </div>

          <div className="p-3 rounded-lg bg-white/[0.03] border border-white/10">
            <span className="text-[11px] text-slate-300 uppercase block font-sans font-semibold">Central Pivot (P)</span>
            <span className="text-lg font-bold text-cyan-300 mt-1 block">{formatPrice(levels.pivot)}</span>
            <span className="text-[10px] text-slate-400 font-sans">Mean Balance</span>
          </div>

          <div className="p-3 rounded-lg bg-indigo-500/[0.06] border border-indigo-500/20">
            <span className="text-[11px] text-indigo-300 uppercase block font-sans font-semibold">Bottom Central (BC)</span>
            <span className="text-lg font-bold text-white mt-1 block">{formatPrice(levels.bottom_central)}</span>
            <span className="text-[10px] text-slate-400 font-sans">Lower Pivot</span>
          </div>

          <div className="p-3 rounded-lg bg-emerald-500/[0.04] border border-emerald-500/20">
            <span className="text-[11px] text-emerald-400 uppercase block font-sans font-semibold">Resistance (R1)</span>
            <span className="text-lg font-bold text-emerald-400 mt-1 block">{formatPrice(levels.r1)}</span>
            <span className="text-[10px] text-slate-400 font-sans">Target 1</span>
          </div>

          <div className="p-3 rounded-lg bg-rose-500/[0.04] border border-rose-500/20">
            <span className="text-[11px] text-rose-400 uppercase block font-sans font-semibold">Support (S1)</span>
            <span className="text-lg font-bold text-rose-400 mt-1 block">{formatPrice(levels.s1)}</span>
            <span className="text-[10px] text-slate-400 font-sans">Stop / Reversal</span>
          </div>

          <div className="p-3 rounded-lg bg-purple-500/[0.04] border border-purple-500/20">
            <span className="text-[11px] text-purple-300 uppercase block font-sans font-semibold">CPR Regime</span>
            <span className="text-base font-extrabold text-white mt-1 block">{levels.regime || 'NEUTRAL'}</span>
            <span className="text-[10px] text-slate-400 font-sans">Width: {levels.cpr_width_pct || '0.00'}%</span>
          </div>
        </div>
      )}

      {strategy === 'dual_ema' && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <div className="p-3 rounded-lg bg-emerald-500/[0.04] border border-emerald-500/20">
            <span className="text-xs text-slate-400 uppercase block font-sans font-semibold">Fast EMA (9-Period)</span>
            <span className="text-xl font-bold text-emerald-400 mt-1 block">{formatPrice(levels.ema_fast)}</span>
            <span className="text-[11px] text-slate-400 font-sans">Momentum Guide</span>
          </div>

          <div className="p-3 rounded-lg bg-rose-500/[0.04] border border-rose-500/20">
            <span className="text-xs text-slate-400 uppercase block font-sans font-semibold">Slow EMA (21-Period)</span>
            <span className="text-xl font-bold text-rose-400 mt-1 block">{formatPrice(levels.ema_slow)}</span>
            <span className="text-[11px] text-slate-400 font-sans">Baseline Baseline</span>
          </div>

          <div className="p-3 rounded-lg bg-indigo-500/[0.04] border border-indigo-500/20">
            <span className="text-xs text-slate-400 uppercase block font-sans font-semibold">Trend Bias</span>
            <span className="text-xl font-bold text-white mt-1 block">{levels.trend || 'NEUTRAL'}</span>
            <span className="text-[11px] text-slate-400 font-sans">Ribbon Slope</span>
          </div>

          <div className="p-3 rounded-lg bg-purple-500/[0.04] border border-purple-500/20">
            <span className="text-xs text-slate-400 uppercase block font-sans font-semibold">Session VWAP</span>
            <span className="text-xl font-bold text-purple-300 mt-1 block">{formatPrice(vwap)}</span>
            <span className="text-[11px] text-slate-400 font-sans">Institutional Fair Value</span>
          </div>
        </div>
      )}

      {strategy === 'orb' && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <div className="p-3 rounded-lg bg-emerald-500/[0.04] border border-emerald-500/20">
            <span className="text-xs text-slate-400 uppercase block font-sans font-semibold">OR High (09:45)</span>
            <span className="text-xl font-bold text-emerald-400 mt-1 block">{formatPrice(levels.orb_high)}</span>
            <span className="text-[11px] text-slate-400 font-sans">Breakout Line</span>
          </div>

          <div className="p-3 rounded-lg bg-rose-500/[0.04] border border-rose-500/20">
            <span className="text-xs text-slate-400 uppercase block font-sans font-semibold">OR Low (09:45)</span>
            <span className="text-xl font-bold text-rose-400 mt-1 block">{formatPrice(levels.orb_low)}</span>
            <span className="text-[11px] text-slate-400 font-sans">Breakdown Line</span>
          </div>

          <div className="p-3 rounded-lg bg-cyan-500/[0.04] border border-cyan-500/20">
            <span className="text-xs text-slate-400 uppercase block font-sans font-semibold">OR Range Width</span>
            <span className="text-xl font-bold text-cyan-300 mt-1 block">₹{levels.orb_width || 0}</span>
            <span className="text-[11px] text-slate-400 font-sans">Points Expansion</span>
          </div>

          <div className="p-3 rounded-lg bg-purple-500/[0.04] border border-purple-500/20">
            <span className="text-xs text-slate-400 uppercase block font-sans font-semibold">Session VWAP</span>
            <span className="text-xl font-bold text-purple-300 mt-1 block">{formatPrice(vwap)}</span>
            <span className="text-[11px] text-slate-400 font-sans">Volume Weighted Price</span>
          </div>
        </div>
      )}
    </div>
  );
}
