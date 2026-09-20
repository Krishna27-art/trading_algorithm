import React, { useState } from 'react';
import { Layers, Filter, RefreshCw, CheckCircle2 } from 'lucide-react';

export default function PositionsTable({ trades, activeTrade, onRefresh }) {
  const [filter, setFilter] = useState('ALL');

  // Combine active trade and historical closed trades
  const allPositions = [];

  if (activeTrade) {
    allPositions.push({
      trade_id: activeTrade.id || 'ACTIVE_01',
      symbol: activeTrade.symbol,
      direction: activeTrade.direction,
      quantity: activeTrade.quantity,
      entry_price: activeTrade.entry_price,
      ltp: activeTrade.current_price,
      initial_stop: activeTrade.stop_loss,
      initial_target: activeTrade.target,
      pnl_net: activeTrade.unrealized_pnl,
      r_multiple: activeTrade.r_multiple,
      status: 'ACTIVE',
    });
  }

  trades.forEach((t) => {
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
    if (filter === 'WINNERS') return p.pnl_net > 0;
    if (filter === 'LOSERS') return p.pnl_net <= 0;
    return true;
  });

  const formatINR = (val) => {
    const num = Math.round(val || 0);
    return (num >= 0 ? '+' : '') + `₹${num.toLocaleString('en-IN')}`;
  };

  return (
    <div className="term-panel p-3.5 space-y-3">
      {/* Header & Filter Buttons */}
      <div className="flex flex-wrap items-center justify-between gap-2 pb-2 border-b border-white/[0.06]">
        <div className="flex items-center gap-2">
          <Layers className="w-4 h-4 text-indigo-400" />
          <h3 className="text-xs font-bold text-slate-100 uppercase tracking-wider">Positions & Trades</h3>
          <span className="text-[10px] text-slate-400 font-mono">({filteredPositions.length})</span>
        </div>

        {/* Filter Pills */}
        <div className="flex items-center gap-1 text-[10px] font-mono bg-black/40 p-0.5 rounded border border-white/5">
          {['ALL', 'ACTIVE', 'CLOSED', 'WINNERS', 'LOSERS'].map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-2 py-0.5 rounded font-medium transition-colors ${
                filter === f ? 'bg-indigo-600 text-white shadow-sm' : 'text-slate-400 hover:text-white'
              }`}
            >
              {f}
            </button>
          ))}
          <button
            onClick={onRefresh}
            className="p-1 rounded text-slate-400 hover:text-white ml-1"
            title="Refresh positions"
          >
            <RefreshCw className="w-3 h-3" />
          </button>
        </div>
      </div>

      {/* Table */}
      <div className="overflow-x-auto max-h-[240px] overflow-y-auto custom-scrollbar">
        <table className="w-full text-left font-mono text-[11px]">
          <thead className="text-[10px] text-slate-400 uppercase border-b border-white/5 pb-1 select-none">
            <tr>
              <th className="py-1.5 font-semibold">Instrument</th>
              <th className="font-semibold">Side</th>
              <th className="font-semibold">Qty</th>
              <th className="font-semibold">Entry</th>
              <th className="font-semibold">LTP / Exit</th>
              <th className="font-semibold">Stop Loss</th>
              <th className="font-semibold">Target</th>
              <th className="font-semibold text-right">P&L</th>
              <th className="font-semibold text-right">R</th>
              <th className="font-semibold text-right pr-1">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/[0.03] text-slate-200">
            {filteredPositions.length === 0 ? (
              <tr>
                <td colSpan={10} className="py-6 text-center text-xs text-slate-500 font-mono">
                  No positions match the "{filter}" filter.
                </td>
              </tr>
            ) : (
              filteredPositions.map((pos, idx) => {
                const isLong = pos.direction === 'BUY';
                const pnl = pos.pnl_net || 0;
                return (
                  <tr key={pos.trade_id || idx} className="hover:bg-white/[0.02] transition-colors">
                    <td className="py-2 text-slate-100 font-bold">{pos.symbol}</td>
                    <td>
                      <span className={`px-1.5 py-0.2 rounded text-[10px] font-bold ${
                        isLong ? 'text-emerald-400 bg-emerald-500/10' : 'text-rose-400 bg-rose-500/10'
                      }`}>
                        {pos.direction}
                      </span>
                    </td>
                    <td>{pos.quantity}</td>
                    <td>₹{pos.entry_price?.toFixed(1)}</td>
                    <td>₹{pos.ltp?.toFixed(1)}</td>
                    <td className="text-rose-400 font-semibold">₹{pos.initial_stop?.toFixed(1)}</td>
                    <td className="text-emerald-400 font-semibold">₹{pos.initial_target?.toFixed(1)}</td>
                    <td className={`text-right font-bold ${pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                      {formatINR(pnl)}
                    </td>
                    <td className="text-right text-slate-300">
                      {pos.r_multiple ? `${pos.r_multiple.toFixed(2)}R` : '-'}
                    </td>
                    <td className="text-right pr-1">
                      <span className={`term-badge text-[9px] ${
                        pos.status === 'ACTIVE' ? 'term-badge-emerald' : 'term-badge-neutral'
                      }`}>
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
  );
}
