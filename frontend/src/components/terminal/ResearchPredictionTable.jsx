import React from 'react';
import {
  TrendingUp,
  TrendingDown,
  Minus,
  CheckCircle2,
  AlertTriangle,
  ArrowRight,
  Zap,
  ShieldCheck,
  Filter,
} from 'lucide-react';

export default function ResearchPredictionTable({
  candidates = [],
  isLoading = false,
  dataSource = 'NONE',
  onSelectCandidate,
  onTradeCandidate,
}) {
  // Helpers for formatting
  const formatPrice = (val) => {
    if (val === null || val === undefined || val === 0) return '—';
    return `₹${Number(val).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  };

  const getPredictionBadge = (pred) => {
    if (!pred || !pred.status) {
      return (
        <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-mono font-medium bg-slate-800/60 text-slate-400 border border-slate-700/40">
          —
        </span>
      );
    }

    const status = pred.status.toUpperCase();
    if (status.includes('LONG') || status.includes('BULLISH')) {
      return (
        <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-mono font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
          <TrendingUp className="w-3 h-3" />
          {status.replace(/_/g, ' ')}
        </span>
      );
    }

    if (status.includes('SHORT') || status.includes('BEARISH')) {
      return (
        <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-mono font-bold bg-rose-500/10 text-rose-400 border border-rose-500/30">
          <TrendingDown className="w-3 h-3" />
          {status.replace(/_/g, ' ')}
        </span>
      );
    }

    if (status.includes('BUFFER')) {
      return (
        <span className="inline-flex items-center px-2.5 py-1 rounded-md text-xs font-mono font-medium bg-amber-500/10 text-amber-300 border border-amber-500/30">
          BUFFER ZONE
        </span>
      );
    }

    if (status.includes('WAITING')) {
      return (
        <span className="inline-flex items-center px-2.5 py-1 rounded-md text-xs font-mono font-medium bg-sky-500/10 text-sky-300 border border-sky-500/30">
          WAITING
        </span>
      );
    }

    return (
      <span className="inline-flex items-center px-2.5 py-1 rounded-md text-xs font-mono font-medium bg-slate-800/80 text-slate-400 border border-slate-700/50">
        NO TRADE
      </span>
    );
  };

  const getConsensusBadge = (consensus) => {
    if (!consensus || !consensus.label) {
      return <span className="text-slate-500 font-mono text-xs">NEUTRAL</span>;
    }

    const label = consensus.label.toUpperCase();
    if (label === 'UNANIMOUS LONG') {
      return (
        <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-md text-xs font-mono font-extrabold bg-emerald-500/20 text-emerald-300 border border-emerald-500/50 shadow-sm shadow-emerald-500/10">
          <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
          UNANIMOUS LONG
        </span>
      );
    }

    if (label === 'UNANIMOUS SHORT') {
      return (
        <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-md text-xs font-mono font-extrabold bg-rose-500/20 text-rose-300 border border-rose-500/50 shadow-sm shadow-rose-500/10">
          <CheckCircle2 className="w-3.5 h-3.5 text-rose-400" />
          UNANIMOUS SHORT
        </span>
      );
    }

    if (label.includes('STRONG LONG')) {
      return (
        <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-mono font-bold bg-emerald-500/15 text-emerald-400 border border-emerald-500/40">
          <TrendingUp className="w-3 h-3" />
          {label}
        </span>
      );
    }

    if (label.includes('STRONG SHORT')) {
      return (
        <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-mono font-bold bg-rose-500/15 text-rose-400 border border-rose-500/40">
          <TrendingDown className="w-3 h-3" />
          {label}
        </span>
      );
    }

    if (label === 'DIVERGENT') {
      return (
        <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-mono font-semibold bg-amber-500/15 text-amber-300 border border-amber-500/40">
          <AlertTriangle className="w-3 h-3 text-amber-400" />
          DIVERGENT
        </span>
      );
    }

    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-mono text-slate-400 bg-slate-800/40 border border-slate-700/30">
        {label}
      </span>
    );
  };

  const getBiasBadge = (bias) => {
    if (bias === 'LONG') {
      return <span className="text-xs font-mono font-bold text-emerald-400">LONG</span>;
    }
    if (bias === 'SHORT') {
      return <span className="text-xs font-mono font-bold text-rose-400">SHORT</span>;
    }
    return <span className="text-xs font-mono text-slate-400">NEUTRAL</span>;
  };

  return (
    <div className="rounded-xl bg-[#0e1422] border border-white/10 shadow-xl overflow-hidden">
      {/* Header Bar */}
      <div className="px-5 py-4 bg-white/[0.02] border-b border-white/10 flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2.5">
            <h2 className="text-base font-bold text-white tracking-wide">
              Multi-Strategy Universe Scan & Live Market Predictions
            </h2>
            <span className="px-2 py-0.5 rounded text-[11px] font-mono font-semibold bg-blue-500/10 text-blue-400 border border-blue-500/20">
              NIFTY 50 Universe
            </span>
          </div>
          <p className="text-xs text-slate-400 mt-0.5">
            Real-time multi-model evaluation: 30m ORB, Central Pivot Range, and Dual-EMA Trend System.
          </p>
        </div>

        <div className="flex items-center gap-2 text-xs font-mono">
          <span className="text-slate-400">Data Source:</span>
          <span
            className={`px-2 py-0.5 rounded font-bold ${
              dataSource === 'REAL_KITE'
                ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30'
                : 'bg-amber-500/10 text-amber-400 border border-amber-500/30'
            }`}
          >
            {dataSource === 'REAL_KITE' ? 'REAL KITE FEED' : dataSource}
          </span>
          <span className="text-slate-500 ml-1">({candidates.length} Stocks)</span>
        </div>
      </div>

      {/* Candidates Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-left border-collapse text-xs sm:text-sm">
          <thead>
            <tr className="border-b border-white/[0.08] bg-white/[0.01] text-[11px] font-mono uppercase text-slate-400 tracking-wider">
              <th className="py-3 px-4 w-12 text-center">Rank</th>
              <th className="py-3 px-4">Stock</th>
              <th className="py-3 px-4 text-right">LTP</th>
              <th className="py-3 px-4 text-center">Momentum</th>
              <th className="py-3 px-4 text-center">Bias</th>
              <th className="py-3 px-4">ORB Prediction</th>
              <th className="py-3 px-4">CPR Prediction</th>
              <th className="py-3 px-4">Dual-EMA Prediction</th>
              <th className="py-3 px-4 text-center">Consensus</th>
              <th className="py-3 px-4 text-right">Action</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/[0.04]">
            {candidates.length === 0 ? (
              <tr>
                <td colSpan={10} className="py-12 text-center text-slate-400">
                  {isLoading ? (
                    <div className="flex items-center justify-center gap-2 font-mono">
                      <div className="w-4 h-4 rounded-full border-2 border-emerald-400 border-t-transparent animate-spin" />
                      Scanning NIFTY 50 universe...
                    </div>
                  ) : (
                    <div className="space-y-1">
                      <p className="font-semibold text-slate-300">No predictions available</p>
                      <p className="text-xs text-slate-500">
                        {dataSource === 'NONE'
                          ? 'Please authenticate Zerodha Kite Connect to stream live predictions.'
                          : 'Waiting for market data refresh.'}
                      </p>
                    </div>
                  )}
                </td>
              </tr>
            ) : (
              candidates.map((cand) => {
                const orb = cand.predictions?.orb;
                const cpr = cand.predictions?.cpr;
                const dual = cand.predictions?.dual_ema;
                const consensus = cand.consensus;

                const hasTradeSignal =
                  (orb && orb.direction) || (cpr && cpr.direction) || (dual && dual.direction);

                return (
                  <tr
                    key={cand.symbol}
                    className="hover:bg-white/[0.02] transition-colors group cursor-pointer"
                    onClick={() => onSelectCandidate && onSelectCandidate(cand.symbol)}
                  >
                    {/* Rank */}
                    <td className="py-3.5 px-4 text-center font-mono font-bold text-slate-400 group-hover:text-white">
                      #{cand.rank}
                    </td>

                    {/* Stock Symbol */}
                    <td className="py-3.5 px-4">
                      <div className="font-bold text-white tracking-wide font-mono text-sm">
                        {cand.symbol}
                      </div>
                      <div className="text-[11px] text-slate-400">NSE Large-Cap</div>
                    </td>

                    {/* LTP */}
                    <td className="py-3.5 px-4 text-right font-mono font-semibold text-slate-100">
                      {formatPrice(cand.ltp)}
                    </td>

                    {/* Momentum Score */}
                    <td className="py-3.5 px-4 text-center">
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-mono font-bold bg-white/[0.04] text-indigo-300 border border-white/5">
                        {cand.momentum_score?.toFixed(1) || '0.0'}
                      </span>
                    </td>

                    {/* Universe Bias */}
                    <td className="py-3.5 px-4 text-center">{getBiasBadge(cand.universe_bias)}</td>

                    {/* ORB Prediction */}
                    <td className="py-3.5 px-4">{getPredictionBadge(orb)}</td>

                    {/* CPR Prediction */}
                    <td className="py-3.5 px-4">{getPredictionBadge(cpr)}</td>

                    {/* Dual-EMA Prediction */}
                    <td className="py-3.5 px-4">{getPredictionBadge(dual)}</td>

                    {/* Consensus */}
                    <td className="py-3.5 px-4 text-center">{getConsensusBadge(consensus)}</td>

                    {/* Action Button */}
                    <td className="py-3.5 px-4 text-right">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          if (onTradeCandidate) onTradeCandidate(cand);
                        }}
                        className={`px-3 py-1.5 rounded-lg text-xs font-mono font-bold tracking-wider transition-all flex items-center gap-1.5 ml-auto ${
                          hasTradeSignal
                            ? 'bg-emerald-500 hover:bg-emerald-400 text-slate-950 shadow-md shadow-emerald-500/20'
                            : 'bg-white/10 hover:bg-white/20 text-slate-200'
                        }`}
                      >
                        <Zap className="w-3.5 h-3.5" />
                        <span>Trade</span>
                      </button>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Legend / Footer info */}
      <div className="px-5 py-3 bg-white/[0.01] border-t border-white/[0.06] flex flex-wrap items-center justify-between gap-3 text-xs text-slate-400">
        <div className="flex flex-wrap items-center gap-4">
          <span className="font-semibold text-slate-300">Labels:</span>
          <span className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full bg-emerald-400" />
            Long Breakout / Bullish Expansion / Trending Long
          </span>
          <span className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full bg-rose-400" />
            Short Breakdown / Bearish Expansion / Trending Short
          </span>
          <span className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full bg-slate-500" />
            No Trade / Buffer Zone
          </span>
        </div>
        <div className="text-[11px] text-slate-500 font-mono">
          Single click row to view chart & deep levels
        </div>
      </div>
    </div>
  );
}
