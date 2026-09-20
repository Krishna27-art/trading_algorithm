import React, { useState } from 'react';
import { Layers, RefreshCw, Filter, ShieldCheck, ArrowUpRight, ArrowDownRight } from 'lucide-react';

export default function PositionsPage({ trades, activeTrade, onRefresh }) {
  const [filter, setFilter] = useState('ALL');

  // Combine active position and closed trades
  const allPositions = [];

  if (activeTrade) {
    allPositions.push({
      trade_id: activeTrade.id || 'ACTIVE_01',
      symbol: activeTrade.symbol || 'NIFTY',
      direction: activeTrade.direction || 'BUY',
      quantity: activeTrade.quantity || 25,
      entry_price: activeTrade.entry_price || 24100.0,
      ltp: activeTrade.current_price || 24135.0,
      initial_stop: activeTrade.stop_loss || 24000.0,
      initial_target: activeTrade.target || 24300.0,
      pnl_net: activeTrade.unrealized_pnl || 875.0,
      r_multiple: activeTrade.r_multiple || 0.35,
      status: 'ACTIVE',
    });
  }

  (trades || []).forEach((t) => {
    allPositions.push({
      ...t,
      ltp: t.exit_price || t.entry_price,
      status: t.exit_price ? 'CLOSED' : 'ACTIVE',
    });
  });

  const filteredPositions = allPositions.filter((p) => {
    if (filter === 'ALL') return true;
    if (filter === 'ACTIVE') return p.status === 'ACTIVE';
    if (filter === 'CLOSED') return p.status === 'CLOSED';
    if (filter === 'WINNERS') return (p.pnl_net || 0) > 0;
    if (filter === 'LOSERS') return (p.pnl_net || 0) <= 0;
    return true;
  });

  const formatINR = (val) => {
    const num = Math.round(val || 0);
    return (num >= 0 ? '+' : '') + `₹${num.toLocaleString('en-IN')}`;
  };

  return (
    <div className="space-y-4">
      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-white/[0.08]">
        <div>
          <h2 className="text-xl font-extrabold text-white tracking-tight flex items-center gap-2.5">
            <Layers className="w-5 h-5 text-indigo-400" /> Positions & Trade Journal
          </h2>
          <p className="text-xs text-slate-400 font-mono mt-0.5">
            Real-time portfolio exposure and historical trade logs
          </p>
        </div>

        {/* Filter Bar */}
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1 text-xs font-mono bg-black/40 p-1 rounded-lg border border-white/10">
            {['ALL', 'ACTIVE', 'CLOSED', 'WINNERS', 'LOSERS'].map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={`px-3 py-1 rounded-md font-bold transition-all ${
                  filter === f
                    ? 'bg-indigo-600 text-white shadow-sm'
                    : 'text-slate-400 hover:text-white hover:bg-white/5'
                }`}
              >
                {f}
              </button>
            ))}
          </div>

          <button
            onClick={onRefresh}
            className="p-2 rounded-lg text-slate-300 hover:text-white hover:bg-white/10 border border-white/5 transition-colors"
            title="Refresh Positions"
          >
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Full Width Table */}
      <div className="term-panel overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-left font-mono text-xs sm:text-sm">
            <thead className="text-xs text-slate-400 uppercase bg-white/[0.02] border-b border-white/[0.08] select-none">
              <tr>
                <th className="py-3 px-4 font-semibold">Instrument</th>
                <th className="py-3 px-4 font-semibold">Direction</th>
                <th className="py-3 px-4 font-semibold">Quantity</th>
                <th className="py-3 px-4 font-semibold">Entry Price</th>
                <th className="py-3 px-4 font-semibold">LTP / Exit</th>
                <th className="py-3 px-4 font-semibold">Stop Loss</th>
                <th className="py-3 px-4 font-semibold">Target (2R)</th>
                <th className="py-3 px-4 font-semibold text-right">Net P&L</th>
                <th className="py-3 px-4 font-semibold text-right">R Multiple</th>
                <th className="py-3 px-4 font-semibold text-right">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/[0.04] text-slate-200">
              {filteredPositions.length === 0 ? (
                <tr>
                  <td colSpan={10} className="py-12 text-center text-sm text-slate-400 font-mono">
                    No positions recorded matching filter "{filter}".
                  </td>
                </tr>
              ) : (
                filteredPositions.map((pos, idx) => {
                  const isLong = pos.direction === 'BUY';
                  const pnl = pos.pnl_net || 0;

                  return (
                    <tr key={pos.trade_id || idx} className="hover:bg-white/[0.03] transition-colors">
                      <td className="py-3.5 px-4 text-white font-extrabold">{pos.symbol}</td>
                      <td className="py-3.5 px-4">
                        <span
                          className={`inline-flex items-center gap-1 px-2.5 py-1 rounded text-xs font-bold ${
                            isLong
                              ? 'text-emerald-400 bg-emerald-500/10 border border-emerald-500/20'
                              : 'text-rose-400 bg-rose-500/10 border border-rose-500/20'
                          }`}
                        >
                          {isLong ? <ArrowUpRight className="w-3.5 h-3.5" /> : <ArrowDownRight className="w-3.5 h-3.5" />}
                          {pos.direction}
                        </span>
                      </td>
                      <td className="py-3.5 px-4 text-slate-300 font-semibold">{pos.quantity}</td>
                      <td className="py-3.5 px-4 font-bold text-white">₹{pos.entry_price?.toFixed(1)}</td>
                      <td className="py-3.5 px-4 font-bold text-cyan-300">₹{pos.ltp?.toFixed(1)}</td>
                      <td className="py-3.5 px-4 text-rose-400 font-bold">₹{pos.initial_stop?.toFixed(1)}</td>
                      <td className="py-3.5 px-4 text-emerald-400 font-bold">₹{pos.initial_target?.toFixed(1)}</td>
                      <td
                        className={`py-3.5 px-4 text-right font-extrabold text-sm ${
                          pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'
                        }`}
                      >
                        {formatINR(pnl)}
                      </td>
                      <td className="py-3.5 px-4 text-right font-bold text-slate-200">
                        {pos.r_multiple !== undefined ? `+${pos.r_multiple.toFixed(2)}R` : '-'}
                      </td>
                      <td className="py-3.5 px-4 text-right">
                        <span
                          className={`term-badge text-xs font-bold ${
                            pos.status === 'ACTIVE' ? 'term-badge-emerald' : 'term-badge-neutral'
                          }`}
                        >
                          {pos.status}
                        </span>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
