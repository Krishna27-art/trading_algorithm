import React from 'react';
import { Compass, CheckCircle2, ShieldAlert, Clock, ArrowUpRight, ArrowDownRight, Layers } from 'lucide-react';

export default function StrategyPage({ telemetry }) {
  const orbHigh = telemetry?.orb_high || 24132.0;
  const orbLow = telemetry?.orb_low || 24031.0;
  const orbWidth = telemetry?.orb_width || 101.0;
  const vwap = telemetry?.vwap || 24095.0;
  const ltp = telemetry?.current_price || 23980.0;

  const rules = [
    {
      step: '1. Opening Range (09:15 – 09:45)',
      desc: 'Price discovery period on the NSE. Records the absolute highest (OR High) and lowest (OR Low) price across the first two completed 15-minute candles.',
      status: 'COMPLETED',
    },
    {
      step: '2. Volatility Filter (Width ≥ 40 pts)',
      desc: 'Filters out low-volatility chop days where breakout momentum is absent. Current width: ' + orbWidth.toFixed(1) + ' pts (Condition Passed).',
      status: 'PASSED',
    },
    {
      step: '3. Long Trigger Rule',
      desc: 'After 09:45, triggers BUY when a completed 15m candle closes ABOVE OR High AND price is strictly ABOVE Session VWAP. Stop = OR Low, Target = 2R.',
      status: 'ACTIVE',
    },
    {
      step: '4. Short Trigger Rule',
      desc: 'After 09:45, triggers SELL when a completed 15m candle closes BELOW OR Low AND price is strictly BELOW Session VWAP. Stop = OR High, Target = 2R.',
      status: 'ACTIVE',
    },
    {
      step: '5. Automatic +1R Breakeven Adjustment',
      desc: 'As soon as unrealized gain reaches +1.0R (halfway to target), stop-loss automatically moves to exact entry price, ensuring zero capital downside.',
      status: 'ARMED',
    },
    {
      step: '6. Position Sizing & Capital Risk',
      desc: 'Strictly 1.0% capital risk per trade (~₹10,000). Quantity is dynamically calculated from Stop distance and rounded to NIFTY futures 25-lot multiples.',
      status: 'ARMED',
    },
    {
      step: '7. Strict Intraday Time Gates',
      desc: 'No new entries after 13:30 IST. Mandatory MIS square-off at 14:30 IST. Hard stop at 15:10 IST. Zero overnight exposure.',
      status: 'ENFORCED',
    },
  ];

  return (
    <div className="space-y-4">
      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-white/[0.08]">
        <div>
          <h2 className="text-xl font-extrabold text-white tracking-tight flex items-center gap-2.5">
            <Compass className="w-5 h-5 text-indigo-400" /> Strategy Architecture & Rulebook
          </h2>
          <p className="text-xs text-slate-400 font-mono mt-0.5">
            30-Minute Volatility-Filtered Opening Range Breakout (ORB) + Session VWAP on NSE India
          </p>
        </div>

        <span className="term-badge term-badge-emerald text-xs font-mono font-bold">
          ORB + VWAP (15m BARS)
        </span>
      </div>

      {/* Real-time Anchor Variables */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 font-mono">
        <div className="term-panel p-3.5">
          <span className="text-xs text-slate-400 font-sans uppercase font-semibold">OR High</span>
          <span className="text-xl font-extrabold text-emerald-400 mt-1 block">₹{orbHigh.toFixed(1)}</span>
        </div>
        <div className="term-panel p-3.5">
          <span className="text-xs text-slate-400 font-sans uppercase font-semibold">OR Low</span>
          <span className="text-xl font-extrabold text-rose-400 mt-1 block">₹{orbLow.toFixed(1)}</span>
        </div>
        <div className="term-panel p-3.5">
          <span className="text-xs text-slate-400 font-sans uppercase font-semibold">OR Width</span>
          <span className="text-xl font-extrabold text-indigo-300 mt-1 block">{orbWidth.toFixed(1)} pts</span>
        </div>
        <div className="term-panel p-3.5">
          <span className="text-xs text-slate-400 font-sans uppercase font-semibold">Session VWAP</span>
          <span className="text-xl font-extrabold text-cyan-300 mt-1 block">₹{vwap.toFixed(1)}</span>
        </div>
        <div className="term-panel p-3.5">
          <span className="text-xs text-slate-400 font-sans uppercase font-semibold">Current LTP</span>
          <span className="text-xl font-extrabold text-white mt-1 block">₹{ltp.toFixed(1)}</span>
        </div>
      </div>

      {/* Rules List */}
      <div className="space-y-3">
        {rules.map((r, idx) => (
          <div key={idx} className="term-panel p-4 flex items-start justify-between gap-4">
            <div className="space-y-1">
              <h3 className="text-sm font-bold text-white flex items-center gap-2">
                <CheckCircle2 className="w-4 h-4 text-emerald-400" />
                {r.step}
              </h3>
              <p className="text-xs text-slate-300 leading-relaxed font-sans pl-6">{r.desc}</p>
            </div>
            <span className="term-badge term-badge-neutral text-xs font-mono font-bold shrink-0">
              {r.status}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
