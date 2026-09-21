import React from 'react';
import {
  TrendingUp,
  TrendingDown,
  Award,
  AlertCircle,
  Zap,
  ArrowUpRight,
  ArrowDownRight,
  Shield,
  Layers,
} from 'lucide-react';

export default function KeyInsightsPanel({ keyInsights, onSelectCandidate }) {
  if (!keyInsights) return null;

  const topLong = keyInsights.top_long;
  const topShort = keyInsights.top_short;
  const strongest = keyInsights.strongest_consensus;
  const divergent =
    keyInsights.divergent_signals && keyInsights.divergent_signals.length > 0
      ? keyInsights.divergent_signals[0]
      : null;

  const renderStrategyReasons = (predictions) => {
    if (!predictions) return null;
    const orb = predictions.orb;
    const cpr = predictions.cpr;
    const dual = predictions.dual_ema;

    return (
      <div className="mt-3 space-y-1.5 border-t border-white/[0.08] pt-2.5 text-xs">
        {orb && (
          <div className="flex items-start gap-1.5">
            <span className="font-mono font-bold text-slate-300 min-w-[55px]">ORB:</span>
            <span className="text-slate-300">
              {orb.reason || orb.status.replace(/_/g, ' ')}
            </span>
          </div>
        )}
        {cpr && (
          <div className="flex items-start gap-1.5">
            <span className="font-mono font-bold text-slate-300 min-w-[55px]">CPR:</span>
            <span className="text-slate-300">
              {cpr.reason || cpr.status.replace(/_/g, ' ')}
            </span>
          </div>
        )}
        {dual && (
          <div className="flex items-start gap-1.5">
            <span className="font-mono font-bold text-slate-300 min-w-[55px]">DUAL-EMA:</span>
            <span className="text-slate-300">
              {dual.reason || dual.status.replace(/_/g, ' ')}
            </span>
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Layers className="w-4 h-4 text-indigo-400" />
        <h3 className="text-sm font-bold text-white uppercase tracking-wider font-mono">
          Key Algorithmic Market Insights
        </h3>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3.5">
        {/* 1. Top Long Opportunity */}
        <div
          onClick={() => topLong && onSelectCandidate && onSelectCandidate(topLong.symbol)}
          className={`p-4 rounded-xl border transition-all cursor-pointer ${
            topLong
              ? 'bg-emerald-500/[0.04] border-emerald-500/30 hover:border-emerald-500/60 shadow-lg shadow-emerald-500/5'
              : 'bg-[#0e1422] border-white/5 opacity-60'
          }`}
        >
          <div className="flex items-center justify-between pb-2 border-b border-white/[0.06]">
            <span className="text-[11px] font-mono font-bold uppercase text-emerald-400 flex items-center gap-1">
              <TrendingUp className="w-3.5 h-3.5" />
              Top Long Opportunity
            </span>
            {topLong && (
              <span className="text-xs font-mono font-bold text-slate-300">
                Score {topLong.momentum_score?.toFixed(1)}
              </span>
            )}
          </div>

          {topLong ? (
            <div className="mt-2.5">
              <div className="flex items-baseline justify-between">
                <span className="text-xl font-black text-white tracking-wide font-mono">
                  {topLong.symbol}
                </span>
                <span className="text-sm font-mono font-semibold text-emerald-400">
                  ₹{topLong.ltp?.toFixed(2)}
                </span>
              </div>
              <div className="mt-1">
                <span className="text-xs font-mono font-extrabold text-emerald-300 bg-emerald-500/15 px-2 py-0.5 rounded border border-emerald-500/30">
                  {topLong.consensus?.label}
                </span>
              </div>
              {renderStrategyReasons(topLong.predictions)}
            </div>
          ) : (
            <div className="py-6 text-center text-xs text-slate-500 font-mono">
              No consensus Long candidates detected
            </div>
          )}
        </div>

        {/* 2. Top Short Opportunity */}
        <div
          onClick={() => topShort && onSelectCandidate && onSelectCandidate(topShort.symbol)}
          className={`p-4 rounded-xl border transition-all cursor-pointer ${
            topShort
              ? 'bg-rose-500/[0.04] border-rose-500/30 hover:border-rose-500/60 shadow-lg shadow-rose-500/5'
              : 'bg-[#0e1422] border-white/5 opacity-60'
          }`}
        >
          <div className="flex items-center justify-between pb-2 border-b border-white/[0.06]">
            <span className="text-[11px] font-mono font-bold uppercase text-rose-400 flex items-center gap-1">
              <TrendingDown className="w-3.5 h-3.5" />
              Top Short Opportunity
            </span>
            {topShort && (
              <span className="text-xs font-mono font-bold text-slate-300">
                Score {topShort.momentum_score?.toFixed(1)}
              </span>
            )}
          </div>

          {topShort ? (
            <div className="mt-2.5">
              <div className="flex items-baseline justify-between">
                <span className="text-xl font-black text-white tracking-wide font-mono">
                  {topShort.symbol}
                </span>
                <span className="text-sm font-mono font-semibold text-rose-400">
                  ₹{topShort.ltp?.toFixed(2)}
                </span>
              </div>
              <div className="mt-1">
                <span className="text-xs font-mono font-extrabold text-rose-300 bg-rose-500/15 px-2 py-0.5 rounded border border-rose-500/30">
                  {topShort.consensus?.label}
                </span>
              </div>
              {renderStrategyReasons(topShort.predictions)}
            </div>
          ) : (
            <div className="py-6 text-center text-xs text-slate-500 font-mono">
              No consensus Short candidates detected
            </div>
          )}
        </div>

        {/* 3. Strongest Consensus */}
        <div
          onClick={() => strongest && onSelectCandidate && onSelectCandidate(strongest.symbol)}
          className={`p-4 rounded-xl border transition-all cursor-pointer ${
            strongest
              ? 'bg-indigo-500/[0.04] border-indigo-500/30 hover:border-indigo-500/60 shadow-lg shadow-indigo-500/5'
              : 'bg-[#0e1422] border-white/5 opacity-60'
          }`}
        >
          <div className="flex items-center justify-between pb-2 border-b border-white/[0.06]">
            <span className="text-[11px] font-mono font-bold uppercase text-indigo-400 flex items-center gap-1">
              <Award className="w-3.5 h-3.5" />
              Strongest Consensus
            </span>
            {strongest && (
              <span className="text-xs font-mono font-bold text-slate-300">
                {strongest.consensus?.agreeing_strategies}/3 Agreement
              </span>
            )}
          </div>

          {strongest ? (
            <div className="mt-2.5">
              <div className="flex items-baseline justify-between">
                <span className="text-xl font-black text-white tracking-wide font-mono">
                  {strongest.symbol}
                </span>
                <span className="text-sm font-mono font-semibold text-slate-200">
                  ₹{strongest.ltp?.toFixed(2)}
                </span>
              </div>
              <div className="mt-1">
                <span className="text-xs font-mono font-extrabold text-indigo-300 bg-indigo-500/15 px-2 py-0.5 rounded border border-indigo-500/30">
                  {strongest.consensus?.label}
                </span>
              </div>
              {renderStrategyReasons(strongest.predictions)}
            </div>
          ) : (
            <div className="py-6 text-center text-xs text-slate-500 font-mono">
              No strong multi-strategy agreement
            </div>
          )}
        </div>

        {/* 4. Most Divergent Stock */}
        <div
          onClick={() => divergent && onSelectCandidate && onSelectCandidate(divergent.symbol)}
          className={`p-4 rounded-xl border transition-all cursor-pointer ${
            divergent
              ? 'bg-amber-500/[0.04] border-amber-500/30 hover:border-amber-500/60 shadow-lg shadow-amber-500/5'
              : 'bg-[#0e1422] border-white/5 opacity-60'
          }`}
        >
          <div className="flex items-center justify-between pb-2 border-b border-white/[0.06]">
            <span className="text-[11px] font-mono font-bold uppercase text-amber-400 flex items-center gap-1">
              <AlertCircle className="w-3.5 h-3.5" />
              Most Divergent Stock
            </span>
            {divergent && (
              <span className="text-xs font-mono font-bold text-amber-300">Conflicting Signals</span>
            )}
          </div>

          {divergent ? (
            <div className="mt-2.5">
              <div className="flex items-baseline justify-between">
                <span className="text-xl font-black text-white tracking-wide font-mono">
                  {divergent.symbol}
                </span>
                <span className="text-sm font-mono font-semibold text-slate-200">
                  ₹{divergent.ltp?.toFixed(2)}
                </span>
              </div>
              <div className="mt-1">
                <span className="text-xs font-mono font-extrabold text-amber-300 bg-amber-500/15 px-2 py-0.5 rounded border border-amber-500/30">
                  DIVERGENT (SIT OUT)
                </span>
              </div>
              {renderStrategyReasons(divergent.predictions)}
            </div>
          ) : (
            <div className="py-6 text-center text-xs text-slate-500 font-mono">
              No opposing strategy conflicts found
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
