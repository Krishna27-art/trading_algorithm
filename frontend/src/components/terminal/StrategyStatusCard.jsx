import React from 'react';
import { Cpu, Activity, Clock, Compass, ArrowUp, ArrowDown } from 'lucide-react';

export default function StrategyStatusCard({ telemetry }) {
  const algoState = telemetry?.algorithm_state || 'WAITING FOR BREAKOUT';
  const orStatus = telemetry?.orb_status || 'COMPLETED';
  const orbHigh = telemetry?.orb_high || 24132.0;
  const orbLow = telemetry?.orb_low || 24031.0;
  const orbWidth = telemetry?.orb_width || 101.0;
  const vwap = telemetry?.vwap || 24095.0;
  const ltp = telemetry?.current_price || 23980.0;

  // Calculate live distances to breakout boundaries
  const distToHigh = orbHigh - ltp;
  const distToLow = ltp - orbLow;

  const getStateBadgeColor = (state) => {
    switch (state) {
      case 'LONG SIGNAL':
      case 'SHORT SIGNAL':
      case 'TRADE ACTIVE':
        return 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30';
      case 'BREAKEVEN':
        return 'text-cyan-300 bg-cyan-500/10 border-cyan-500/30';
      case 'TARGET HIT':
        return 'text-indigo-300 bg-indigo-500/10 border-indigo-500/30';
      case 'STOP LOSS':
      case 'STOP LOSS HIT':
      case 'DAILY LIMIT REACHED':
        return 'text-rose-400 bg-rose-500/10 border-rose-500/30';
      case 'BUILDING OR':
      case 'WAITING':
      case 'WAITING FOR BREAKOUT':
        return 'text-amber-400 bg-amber-500/10 border-amber-500/30';
      default:
        return 'text-slate-300 bg-white/5 border-white/10';
    }
  };

  const getStateExplanation = (state) => {
    switch (state) {
      case 'INITIALIZING':
        return 'Establishing broker websocket feed and fetching previous close anchors.';
      case 'BUILDING OR':
        return '09:15–09:45 price discovery window active. Establishing high & low boundaries.';
      case 'WAITING':
      case 'WAITING FOR BREAKOUT':
        return 'Opening Range established. Scanning 15m candle closes outside OR levels with VWAP confirmation.';
      case 'LONG SIGNAL':
        return 'Bullish trigger: Completed 15m candle closed above OR High and above Session VWAP.';
      case 'SHORT SIGNAL':
        return 'Bearish trigger: Completed 15m candle closed below OR Low and below Session VWAP.';
      case 'TRADE ACTIVE':
        return 'Trade executed. Trailing stop armed with automatic +1R breakeven shift.';
      case 'BREAKEVEN':
        return 'Target +1R reached! Stop-loss moved to entry price (Zero downside risk).';
      case 'TARGET HIT':
        return '2.0R profit objective reached. Trade closed in profit.';
      case 'STOP LOSS':
      case 'STOP LOSS HIT':
        return 'Protective stop-loss triggered. Trade concluded within 1% risk limit.';
      case 'DAILY LIMIT REACHED':
        return '2.0% daily risk kill-switch engaged. All trading halted for the day.';
      case 'MARKET CLOSED':
        return 'Market session concluded. All intraday positions squared off.';
      default:
        return 'Algorithmic state monitoring active.';
    }
  };

  return (
    <div className="term-panel p-4 space-y-3.5">
      {/* Header */}
      <div className="flex items-center justify-between pb-2.5 border-b border-white/[0.08]">
        <div className="flex items-center gap-2">
          <Cpu className="w-4 h-4 text-indigo-400" />
          <h3 className="text-sm font-bold text-white uppercase tracking-wider">Strategy Status</h3>
        </div>
        <span className="text-xs font-mono text-slate-400">09:15 – 15:10 IST</span>
      </div>

      {/* Large Readable State Indicator */}
      <div className={`p-3 rounded-lg border flex items-center justify-between ${getStateBadgeColor(algoState)}`}>
        <div>
          <span className="text-xs uppercase font-sans font-semibold text-slate-400 block">Current State</span>
          <span className="text-xl sm:text-2xl font-black font-mono tracking-tight">
            {algoState}
          </span>
        </div>
        <Activity className="w-6 h-6 shrink-0 opacity-80" />
      </div>

      {/* Grid of Strategy Metrics */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5 font-mono text-xs">
        <div className="p-2.5 rounded-lg bg-white/[0.02] border border-white/5">
          <span className="text-[11px] text-slate-400 font-sans block uppercase">Opening Range</span>
          <span className="text-sm font-bold text-emerald-400 mt-1 block">{orStatus}</span>
        </div>

        <div className="p-2.5 rounded-lg bg-white/[0.02] border border-white/5">
          <span className="text-[11px] text-slate-400 font-sans block uppercase">OR High</span>
          <span className="text-sm font-bold text-white mt-1 block">₹{orbHigh.toFixed(1)}</span>
        </div>

        <div className="p-2.5 rounded-lg bg-white/[0.02] border border-white/5">
          <span className="text-[11px] text-slate-400 font-sans block uppercase">OR Low</span>
          <span className="text-sm font-bold text-white mt-1 block">₹{orbLow.toFixed(1)}</span>
        </div>

        <div className="p-2.5 rounded-lg bg-white/[0.02] border border-white/5">
          <span className="text-[11px] text-slate-400 font-sans block uppercase">OR Width</span>
          <span className="text-sm font-bold text-indigo-300 mt-1 block">{orbWidth.toFixed(1)} pts</span>
        </div>

        <div className="p-2.5 rounded-lg bg-white/[0.02] border border-white/5">
          <span className="text-[11px] text-slate-400 font-sans block uppercase">Session VWAP</span>
          <span className="text-sm font-bold text-cyan-300 mt-1 block">₹{vwap.toFixed(1)}</span>
        </div>

        <div className="p-2.5 rounded-lg bg-white/[0.02] border border-white/5">
          <span className="text-[11px] text-slate-400 font-sans block uppercase">Current LTP</span>
          <span className="text-sm font-bold text-white mt-1 block">₹{ltp.toFixed(1)}</span>
        </div>

        <div className="p-2.5 rounded-lg bg-emerald-500/[0.03] border border-emerald-500/20">
          <span className="text-[11px] text-slate-400 font-sans block uppercase flex items-center gap-1">
            <ArrowUp className="w-3 h-3 text-emerald-400" /> Dist to OR High
          </span>
          <span className="text-sm font-bold text-emerald-400 mt-1 block">
            {distToHigh > 0 ? `+${distToHigh.toFixed(1)} pts` : 'BROKEN OUT'}
          </span>
        </div>

        <div className="p-2.5 rounded-lg bg-rose-500/[0.03] border border-rose-500/20">
          <span className="text-[11px] text-slate-400 font-sans block uppercase flex items-center gap-1">
            <ArrowDown className="w-3 h-3 text-rose-400" /> Dist to OR Low
          </span>
          <span className="text-sm font-bold text-rose-400 mt-1 block">
            {distToLow > 0 ? `-${distToLow.toFixed(1)} pts` : 'BROKEN DOWN'}
          </span>
        </div>
      </div>

      {/* Beginner Explanation */}
      <div className="p-3 rounded-lg bg-black/40 border border-white/5 flex items-start gap-2.5 text-xs text-slate-300">
        <Compass className="w-4 h-4 text-indigo-400 shrink-0 mt-0.5" />
        <p className="leading-relaxed">{getStateExplanation(algoState)}</p>
      </div>
    </div>
  );
}
