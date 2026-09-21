import React from 'react';
import { Cpu, Activity, Clock, Compass, ArrowUp, ArrowDown } from 'lucide-react';

export default function StrategyStatusCard({ telemetry }) {
  const strategy = (telemetry?.strategy || 'cpr').toUpperCase();
  const algoState = telemetry?.algorithm_state || 'SCANNING';
  const levels = telemetry?.strategy_levels || {};
  const ltp = telemetry?.current_price || 0.0;
  const vwap = telemetry?.vwap || 0.0;

  const getStateBadgeColor = (state) => {
    const s = (state || '').toUpperCase();
    if (s.includes('LONG') || s.includes('BUY') || s.includes('BULLISH') || s.includes('ACTIVE') || s.includes('TARGET')) {
      return 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30';
    }
    if (s.includes('SHORT') || s.includes('SELL') || s.includes('BEARISH') || s.includes('STOP') || s.includes('LOSS')) {
      return 'text-rose-400 bg-rose-500/10 border-rose-500/30';
    }
    if (s.includes('NARROW') || s.includes('BREAKOUT')) {
      return 'text-indigo-300 bg-indigo-500/10 border-indigo-500/30';
    }
    return 'text-amber-400 bg-amber-500/10 border-amber-500/30';
  };

  const getExplanation = () => {
    if (strategy === 'CPR') {
      const regime = levels.regime || 'NEUTRAL';
      if (regime === 'NARROW') {
        return `Narrow CPR regime detected (Width: ${levels.cpr_width_pct || '0'}%). High probability breakout day — confirming 15m candle close outside TC (${levels.top_central || '—'}) / BC (${levels.bottom_central || '—'}).`;
      }
      if (regime === 'WIDE') {
        return `Wide CPR regime detected (Width: ${levels.cpr_width_pct || '0'}%). Range-bound conditions expected — evaluating mean-reversion fades toward Central Pivot P.`;
      }
      return `Neutral CPR width (${levels.cpr_width_pct || '0'}%). Evaluating directional trend confirmation against Session VWAP.`;
    }

    if (strategy === 'DUAL_EMA') {
      return `Dual EMA trend system tracking 9-EMA (${levels.ema_fast || '—'}) vs 21-EMA (${levels.ema_slow || '—'}). Trend bias: ${levels.trend || 'NEUTRAL'}.`;
    }

    return 'Evaluating 30-min Opening Range high/low boundaries with VWAP volume confirmation.';
  };

  return (
    <div className="term-panel p-3.5 space-y-3 font-mono">
      {/* Header */}
      <div className="flex items-center justify-between pb-2 border-b border-white/[0.06]">
        <div className="flex items-center gap-2">
          <Cpu className="w-4 h-4 text-indigo-400" />
          <h3 className="text-xs font-bold text-slate-100 uppercase tracking-wider">
            {strategy} State Machine
          </h3>
        </div>
        <span className={`text-[10px] font-bold px-2 py-0.5 rounded border uppercase ${getStateBadgeColor(algoState)}`}>
          {algoState}
        </span>
      </div>

      {/* State Explanation Box */}
      <div className="p-2.5 rounded bg-black/40 border border-white/[0.06] text-xs text-slate-300 leading-relaxed font-sans">
        <p>{getExplanation()}</p>
      </div>

      {/* Micro-metrics row */}
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="p-2 rounded bg-white/[0.02] border border-white/5">
          <span className="text-[10px] text-slate-400 block uppercase font-sans">Current LTP</span>
          <span className="text-sm font-bold text-white mt-0.5 block">
            {ltp ? `₹${Number(ltp).toFixed(2)}` : '—'}
          </span>
        </div>
        <div className="p-2 rounded bg-white/[0.02] border border-white/5">
          <span className="text-[10px] text-slate-400 block uppercase font-sans">Session VWAP</span>
          <span className="text-sm font-bold text-purple-300 mt-0.5 block">
            {vwap ? `₹${Number(vwap).toFixed(2)}` : '—'}
          </span>
        </div>
      </div>
    </div>
  );
}
