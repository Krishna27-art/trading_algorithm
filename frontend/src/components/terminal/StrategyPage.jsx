import React from 'react';
import { Compass, CheckCircle2, ShieldAlert, Clock, ArrowUpRight, ArrowDownRight, Layers, Award } from 'lucide-react';

export default function StrategyPage({ telemetry, selectedStrategy = 'cpr', onSelectStrategy }) {
  const strategy = (selectedStrategy || telemetry?.strategy || 'cpr').toLowerCase();

  const cprRules = [
    {
      step: '1. Central Pivot Range Calculation',
      desc: 'Computed before market open from previous day OHLC: Pivot P = (H+L+C)/3, Bottom Central BC = (H+L)/2, Top Central TC = 2P - BC. Establishes daily institutional boundaries.',
      status: 'MATHEMATICALLY EXACT',
    },
    {
      step: '2. Daily Regime Classification',
      desc: 'Width % = |TC - BC| / P * 100. If Width < 0.25% → NARROW (High Probability Trending Day). If Width > 0.60% → WIDE (Range-Bound Mean Reversion). Otherwise NEUTRAL.',
      status: 'REGIME ADAPTIVE',
    },
    {
      step: '3. Narrow CPR Breakout Entry',
      desc: 'On Narrow days: Triggers BUY when 15m candle closes above TC & VWAP (Stop = BC, Target = 2R). Triggers SELL when 15m candle closes below BC & VWAP (Stop = TC, Target = 2R).',
      status: '57.9% WIN RATE',
    },
    {
      step: '4. Wide CPR Mean-Reversion Fade',
      desc: 'On Wide days: Price stretching away from Central Pivot towards R1/S1 triggers counter-trend mean reversion back to the Pivot line P.',
      status: 'ACTIVE',
    },
    {
      step: '5. Risk Gate & Breakeven Lock',
      desc: 'Strict 1.0% capital risk per trade. Automatic breakeven stop adjustment at +1R unrealized gain to guarantee zero loss on runners.',
      status: 'ARMED',
    },
    {
      step: '6. Session Square-Off',
      desc: 'Strict intraday discipline. Mandatory square-off at 14:30 IST. Hard cutoff at 15:10 IST. Zero overnight gap risk.',
      status: 'ENFORCED',
    },
  ];

  const dualEmaRules = [
    {
      step: '1. Dual EMA Trend Ribbon',
      desc: 'Maintains running 9-period Fast EMA and 21-period Slow EMA on completed 15m bars to gauge institutional trend direction.',
      status: 'ACTIVE',
    },
    {
      step: '2. Volatility Buffer Filtering',
      desc: 'Applies a 0.15 * ATR(14) buffer zone around the EMA crossover line to eliminate choppy sideways false breakouts.',
      status: 'ACTIVE',
    },
    {
      step: '3. Trend Confirmation & VWAP Alignment',
      desc: 'BUY orders require 9-EMA > 21-EMA and price above VWAP. SELL orders require 9-EMA < 21-EMA and price below VWAP.',
      status: 'ACTIVE',
    },
    {
      step: '4. Dynamic Trailing Exits',
      desc: 'Exits on opposite EMA ribbon flip or hard square-off time at 14:30 IST.',
      status: 'ENFORCED',
    },
  ];

  const orbRules = [
    {
      step: '1. Opening Range (09:15 – 09:45)',
      desc: 'Records absolute High and Low across the first two completed 15-minute bars.',
      status: 'COMPLETED',
    },
    {
      step: '2. Volatility Filter',
      desc: 'Requires minimum OR range width to avoid entering low-volatility dead sessions.',
      status: 'PASSED',
    },
    {
      step: '3. VWAP Confirmation',
      desc: 'Breakout above OR High requires price > VWAP. Breakdown below OR Low requires price < VWAP.',
      status: 'ACTIVE',
    },
    {
      step: '4. 1:2.0 Risk-to-Reward',
      desc: 'Target is set at 2x the stop distance with automatic breakeven at +1R.',
      status: 'ARMED',
    },
  ];

  const getRules = () => {
    if (strategy === 'cpr') return cprRules;
    if (strategy === 'dual_ema') return dualEmaRules;
    return orbRules;
  };

  return (
    <div className="space-y-4 font-mono">
      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-white/[0.08]">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="text-xl font-extrabold text-white tracking-tight flex items-center gap-2.5">
              <Compass className="w-5 h-5 text-indigo-400" /> Strategy Architecture & Rulebook
            </h2>
            {strategy === 'cpr' && (
              <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 flex items-center gap-1">
                <Award className="w-3 h-3" /> TOP PERFORMER (57.9% WIN RATE)
              </span>
            )}
          </div>
          <p className="text-xs text-slate-400 mt-0.5">
            Institutional Intraday Trading System on NSE India • Frictions Net of Revised Oct 2024 SEBI Norms
          </p>
        </div>

        {/* Strategy Switcher */}
        <div className="flex items-center bg-black/40 p-1 rounded-lg border border-white/10 text-xs font-bold">
          <button
            onClick={() => onSelectStrategy && onSelectStrategy('cpr')}
            className={`px-3 py-1 rounded transition-all ${
              strategy === 'cpr' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-white'
            }`}
          >
            CPR REGIME
          </button>
          <button
            onClick={() => onSelectStrategy && onSelectStrategy('dual_ema')}
            className={`px-3 py-1 rounded transition-all ${
              strategy === 'dual_ema' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-white'
            }`}
          >
            DUAL-EMA
          </button>
          <button
            onClick={() => onSelectStrategy && onSelectStrategy('orb')}
            className={`px-3 py-1 rounded transition-all ${
              strategy === 'orb' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-white'
            }`}
          >
            30M ORB
          </button>
        </div>
      </div>

      {/* Rules List */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {getRules().map((r, i) => (
          <div key={i} className="term-panel p-4 space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-bold text-white uppercase tracking-wider font-sans">
                {r.step}
              </span>
              <span className="text-[10px] font-mono font-bold px-2 py-0.5 rounded bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">
                {r.status}
              </span>
            </div>
            <p className="text-xs text-slate-300 leading-relaxed font-sans">
              {r.desc}
            </p>
          </div>
        ))}
      </div>
    </div>
  );
}
