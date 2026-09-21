import React from 'react';
import { Compass, TrendingUp, TrendingDown, ArrowRight, RefreshCw, BarChart2, Zap, ShieldCheck } from 'lucide-react';

export default function ScannerPage({ scannerData, isLoading, onRefresh, onSelectStock }) {
  const candidates = scannerData?.candidates || [];
  const dataSource = scannerData?.data_source || 'NONE';

  const formatINR = (val) => `₹${(val || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

  return (
    <div className="space-y-4 animate-fade-in">
      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-white/[0.08]">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="text-xl font-extrabold text-white tracking-tight flex items-center gap-2.5">
              <Compass className="w-5 h-5 text-indigo-400" /> NIFTY 50 Universe Scanner & Predictive Ranking
            </h2>
            <span className={`text-[10px] font-mono px-2 py-0.5 rounded font-bold ${
              dataSource === 'REAL' ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30' : 'bg-slate-500/20 text-slate-400 border border-slate-500/30'
            }`}>
              DATA FEED: {dataSource}
            </span>
          </div>
          <p className="text-xs text-slate-400 font-mono mt-0.5">
            Transparent 100-Point Momentum & Breakout Score (RVOL 30% • Gap 25% • ATR Volatility 25% • VWAP Clearance 20%)
          </p>
        </div>

        <button
          onClick={onRefresh}
          disabled={isLoading}
          className="term-btn-primary py-2 px-4 text-xs font-mono font-bold"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${isLoading ? 'animate-spin' : ''}`} />
          <span>{isLoading ? 'Scanning Universe...' : 'Refresh Quotes'}</span>
        </button>
      </div>

      {/* Top 3 Summary Pills */}
      {candidates.length >= 3 && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          {candidates.slice(0, 3).map((item, idx) => (
            <div
              key={item.symbol}
              onClick={() => onSelectStock && onSelectStock(item.symbol)}
              className="term-panel p-3.5 cursor-pointer hover:border-indigo-500/50 transition-all group bg-gradient-to-br from-white/[0.02] to-indigo-500/[0.04]"
            >
              <div className="flex items-center justify-between">
                <span className="text-[11px] font-mono font-bold text-slate-400 uppercase">
                  RANK #{item.rank || idx + 1} PICK
                </span>
                <span className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded ${
                  item.direction_bias === 'LONG' ? 'bg-emerald-500/20 text-emerald-300' :
                  item.direction_bias === 'SHORT' ? 'bg-rose-500/20 text-rose-300' : 'bg-slate-500/20 text-slate-300'
                }`}>
                  {item.direction_bias}
                </span>
              </div>
              <div className="flex items-baseline justify-between mt-2">
                <span className="text-base font-extrabold text-white group-hover:text-indigo-300 font-mono">
                  {item.symbol}
                </span>
                <span className="text-sm font-black text-indigo-400 font-mono">
                  {item.total_score} / 100
                </span>
              </div>
              <div className="flex items-center justify-between text-xs text-slate-400 mt-1 font-mono">
                <span>LTP: {formatINR(item.ltp)}</span>
                <span className={item.gap_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'}>
                  Gap: {item.gap_pct > 0 ? `+${item.gap_pct}%` : `${item.gap_pct}%`}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Full Candidates Ranking Table */}
      <div className="term-panel overflow-hidden">
        <div className="p-3 border-b border-white/[0.06] flex items-center justify-between">
          <span className="text-xs font-bold text-slate-200 uppercase tracking-wider font-mono">
            Candidate Standings ({candidates.length} Stocks Evaluated)
          </span>
          <span className="text-[11px] text-slate-400 font-mono">
            Click symbol to load charts and execute on terminal
          </span>
        </div>

        {candidates.length === 0 ? (
          <div className="p-12 text-center space-y-3 font-mono">
            <Compass className="w-8 h-8 mx-auto text-slate-600 animate-pulse" />
            <p className="text-sm text-slate-400 font-medium">
              {dataSource === 'NONE'
                ? 'Zerodha Kite session required to stream live NIFTY 50 quotes. Please log in or generate session.'
                : 'No scanner candidates returned. Click "Refresh Quotes" to scan.'}
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs font-mono">
              <thead className="bg-white/[0.02] text-slate-400 border-b border-white/[0.06] text-[11px] uppercase">
                <tr>
                  <th className="py-2.5 px-3">Rank</th>
                  <th className="py-2.5 px-3">Stock Symbol</th>
                  <th className="py-2.5 px-3 text-right">LTP (₹)</th>
                  <th className="py-2.5 px-3 text-right">Gap %</th>
                  <th className="py-2.5 px-3 text-right">RVOL (20d)</th>
                  <th className="py-2.5 px-3 text-right">ATR (14)</th>
                  <th className="py-2.5 px-3 text-right">VWAP Dist %</th>
                  <th className="py-2.5 px-3 text-center">Bias</th>
                  <th className="py-2.5 px-3 text-right">Score</th>
                  <th className="py-2.5 px-3 text-center">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/[0.04]">
                {candidates.map((c, i) => {
                  const isLong = c.direction_bias === 'LONG';
                  const isShort = c.direction_bias === 'SHORT';

                  return (
                    <tr
                      key={c.symbol}
                      className="hover:bg-indigo-500/[0.06] transition-colors cursor-pointer"
                      onClick={() => onSelectStock && onSelectStock(c.symbol)}
                    >
                      <td className="py-2.5 px-3 text-slate-400 font-bold">#{c.rank || i + 1}</td>
                      <td className="py-2.5 px-3 font-bold text-white flex items-center gap-1.5">
                        <span>{c.symbol}</span>
                      </td>
                      <td className="py-2.5 px-3 text-right text-slate-200">{formatINR(c.ltp)}</td>
                      <td className={`py-2.5 px-3 text-right font-semibold ${c.gap_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {c.gap_pct > 0 ? `+${c.gap_pct}%` : `${c.gap_pct}%`}
                      </td>
                      <td className="py-2.5 px-3 text-right text-slate-300 font-semibold">{c.rvol}x</td>
                      <td className="py-2.5 px-3 text-right text-slate-400">
                        ₹{c.atr_14} ({c.atr_pct}%)
                      </td>
                      <td className={`py-2.5 px-3 text-right font-semibold ${c.vwap_dist_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {c.vwap_dist_pct > 0 ? `+${c.vwap_dist_pct}%` : `${c.vwap_dist_pct}%`}
                      </td>
                      <td className="py-2.5 px-3 text-center">
                        <span className={`text-[10px] font-bold px-2 py-0.5 rounded ${
                          isLong ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30' :
                          isShort ? 'bg-rose-500/20 text-rose-300 border border-rose-500/30' :
                          'bg-slate-500/20 text-slate-400 border border-slate-500/30'
                        }`}>
                          {c.direction_bias}
                        </span>
                      </td>
                      <td className="py-2.5 px-3 text-right">
                        <div className="flex items-center justify-end gap-2">
                          <div className="w-12 bg-white/10 h-1.5 rounded-full overflow-hidden">
                            <div
                              className="h-full bg-indigo-400 rounded-full"
                              style={{ width: `${Math.min(c.total_score, 100)}%` }}
                            />
                          </div>
                          <span className="font-extrabold text-indigo-300 font-mono">{c.total_score}</span>
                        </div>
                      </td>
                      <td className="py-2.5 px-3 text-center">
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            if (onSelectStock) onSelectStock(c.symbol);
                          }}
                          className="px-2.5 py-1 text-[10px] font-mono font-bold bg-indigo-600 hover:bg-indigo-500 text-white rounded transition-colors inline-flex items-center gap-1"
                        >
                          <span>Terminal</span>
                          <ArrowRight className="w-3 h-3" />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
