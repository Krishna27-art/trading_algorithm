import React, { useState } from 'react';
import {
  Layers,
  BookOpen,
  RefreshCw,
  ShieldCheck,
  AlertTriangle,
  ArrowUpRight,
  ArrowDownRight,
  CheckCircle2,
} from 'lucide-react';


export default function PositionsPage({
  positionsData,
  trades = [],
  tradingMode = 'PAPER',
  onRefresh,
}) {
  const [activeSubTab, setActiveSubTab] = useState('broker'); // 'broker' | 'journal'
  const [journalFilter, setJournalFilter] = useState('ALL'); // 'ALL' | 'WINNERS' | 'LOSERS'
  const [squaringOffSymbol, setSquaringOffSymbol] = useState(null);

  const brokerPositions = positionsData?.positions || [];
  const totalUnrealised = positionsData?.total_unrealised_pnl || 0.0;
  const totalRealised = positionsData?.total_realised_pnl || 0.0;
  const totalPnl = positionsData?.total_pnl || 0.0;
  const brokerMode = positionsData?.broker || tradingMode;

  const formatINR = (val) => {
    const num = Number(val || 0);
    return (num >= 0 ? '+' : '') + `₹${num.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  };

  const handleSquareOff = async (symbol) => {
    if (!window.confirm(`Confirm immediate square-off for ${symbol} on broker?`)) {
      return;
    }
    setSquaringOffSymbol(symbol);
    try {
      const res = await fetch('/api/orders/exit', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Shared-Secret': localStorage.getItem('app_shared_secret') || '',
        },
        body: JSON.stringify({
          symbol,
          mode: tradingMode,
        }),
      });
      const data = await res.json();
      if (data.success) {
        alert(data.message || `Squared off ${symbol}`);
        if (onRefresh) onRefresh();
      } else {
        alert(`Square-off failed: ${data.detail || 'Unknown broker error'}`);
      }
    } catch (e) {
      alert(`Network error squaring off: ${e.message}`);
    } finally {
      setSquaringOffSymbol(null);
    }
  };

  const filteredTrades = trades.filter((t) => {
    if (journalFilter === 'WINNERS') return (t.pnl_net || 0) > 0;
    if (journalFilter === 'LOSERS') return (t.pnl_net || 0) <= 0;
    return true;
  });

  return (
    <div className="space-y-4">
      {/* Top Header & Sub-tab Switcher */}
      <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-white/[0.08]">
        <div>
          <div className="flex items-center gap-3">
            <h2 className="text-xl font-extrabold text-white tracking-tight flex items-center gap-2.5">
              <Layers className="w-5 h-5 text-indigo-400" /> Portfolio Positions & Trade Journal
            </h2>
            <span
              className={`px-2.5 py-0.5 rounded text-[11px] font-mono font-bold border ${
                brokerMode === 'LIVE'
                  ? 'bg-emerald-500/10 text-emerald-300 border-emerald-500/20'
                  : 'bg-amber-500/10 text-amber-300 border-amber-500/20'
              }`}
            >
              {brokerMode === 'LIVE' ? 'LIVE BROKER FEED' : 'PAPER BROKER FEED'}
            </span>
          </div>
          <p className="text-xs text-slate-400 font-mono mt-1">
            Broker net positions are isolated from historical execution logs
          </p>
        </div>

        {/* Action Buttons & Subtab Switcher */}
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1 bg-[#0f172a] p-1 rounded-lg border border-white/10">
            <button
              onClick={() => setActiveSubTab('broker')}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-bold transition-all ${
                activeSubTab === 'broker'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-white hover:bg-white/5'
              }`}
            >
              <Layers className="w-3.5 h-3.5" />
              Broker Positions ({brokerPositions.length})
            </button>
            <button
              onClick={() => setActiveSubTab('journal')}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-bold transition-all ${
                activeSubTab === 'journal'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-white hover:bg-white/5'
              }`}
            >
              <BookOpen className="w-3.5 h-3.5" />
              Trade Journal ({trades.length})
            </button>
          </div>

          <button
            onClick={onRefresh}
            className="p-2 rounded-lg text-slate-300 hover:text-white hover:bg-white/10 border border-white/5 transition-colors"
            title="Refresh Portfolio"
          >
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* SUBTAB 1: LIVE BROKER NET POSITIONS */}
      {activeSubTab === 'broker' && (
        <div className="space-y-4">
          {/* Summary KPIs */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <div className="term-panel p-3">
              <div className="text-[11px] font-mono text-slate-400 uppercase">Open Positions</div>
              <div className="text-xl font-extrabold text-white font-mono mt-0.5">
                {brokerPositions.length}
              </div>
            </div>

            <div className="term-panel p-3">
              <div className="text-[11px] font-mono text-slate-400 uppercase">Unrealised P&L</div>
              <div
                className={`text-xl font-extrabold font-mono mt-0.5 ${
                  totalUnrealised >= 0 ? 'text-emerald-400' : 'text-rose-400'
                }`}
              >
                {formatINR(totalUnrealised)}
              </div>
            </div>

            <div className="term-panel p-3">
              <div className="text-[11px] font-mono text-slate-400 uppercase">Realised P&L</div>
              <div
                className={`text-xl font-extrabold font-mono mt-0.5 ${
                  totalRealised >= 0 ? 'text-emerald-400' : 'text-rose-400'
                }`}
              >
                {formatINR(totalRealised)}
              </div>
            </div>

            <div className="term-panel p-3">
              <div className="text-[11px] font-mono text-slate-400 uppercase">Total Net P&L</div>
              <div
                className={`text-xl font-extrabold font-mono mt-0.5 ${
                  totalPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'
                }`}
              >
                {formatINR(totalPnl)}
              </div>
            </div>
          </div>

          {/* Table */}
          <div className="term-panel overflow-hidden">
            <div className="px-4 py-3 border-b border-white/[0.08] flex items-center justify-between">
              <div className="text-xs font-bold uppercase tracking-wider text-slate-300 font-mono flex items-center gap-2">
                <ShieldCheck className="w-4 h-4 text-emerald-400" />
                Live Broker Net Positions (Single Source of Truth)
              </div>
              <div className="text-[11px] text-slate-400 font-mono">
                1 Row Per (Exchange + Symbol + Product)
              </div>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-left font-mono text-xs sm:text-sm">
                <thead className="text-[11px] text-slate-400 uppercase bg-white/[0.02] border-b border-white/[0.08] select-none">
                  <tr>
                    <th className="py-3 px-4 font-semibold">Instrument</th>
                    <th className="py-3 px-4 font-semibold">Product</th>
                    <th className="py-3 px-4 font-semibold">Side</th>
                    <th className="py-3 px-4 font-semibold">Net Qty</th>
                    <th className="py-3 px-4 font-semibold">Avg Price</th>
                    <th className="py-3 px-4 font-semibold">LTP</th>
                    <th className="py-3 px-4 font-semibold text-right">Unrealised P&L</th>
                    <th className="py-3 px-4 font-semibold text-right">Realised P&L</th>
                    <th className="py-3 px-4 font-semibold">Strategy SL</th>
                    <th className="py-3 px-4 font-semibold">Strategy Target</th>
                    <th className="py-3 px-4 font-semibold text-center">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-white/[0.04] text-slate-200">
                  {brokerPositions.length === 0 ? (
                    <tr>
                      <td colSpan={11} className="py-12 text-center text-sm text-slate-400 font-mono">
                        <CheckCircle2 className="w-8 h-8 text-emerald-400 mx-auto mb-2 opacity-50" />
                        No active open positions on broker. Net exposure is flat.
                      </td>
                    </tr>
                  ) : (
                    brokerPositions.map((pos) => {
                      const isLong = pos.quantity > 0;
                      const uPnl = pos.unrealised_pnl || 0.0;
                      const rPnl = pos.realised_pnl || 0.0;

                      return (
                        <tr
                          key={pos.position_id}
                          className={`hover:bg-white/[0.03] transition-colors ${
                            pos.is_quarantined ? 'bg-rose-950/20' : ''
                          }`}
                        >
                          <td className="py-3.5 px-4 font-extrabold text-white">
                            <div className="flex items-center gap-2">
                              <span>{pos.tradingsymbol}</span>
                              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-white/5 border border-white/10 text-slate-400">
                                {pos.exchange}
                              </span>
                              {pos.is_quarantined && (
                                <span
                                  className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-rose-500/20 text-rose-300 border border-rose-500/30 flex items-center gap-1"
                                  title={pos.quarantine_reason}
                                >
                                  <AlertTriangle className="w-3 h-3" /> QUARANTINED
                                </span>
                              )}
                            </div>
                          </td>

                          <td className="py-3.5 px-4 text-slate-300 font-mono font-semibold">
                            <span className="px-2 py-0.5 rounded bg-indigo-500/10 text-indigo-300 border border-indigo-500/20 text-xs">
                              {pos.product}
                            </span>
                          </td>

                          <td className="py-3.5 px-4">
                            <span
                              className={`inline-flex items-center gap-1 px-2.5 py-0.5 rounded text-xs font-bold ${
                                isLong
                                  ? 'text-emerald-400 bg-emerald-500/10 border border-emerald-500/20'
                                  : 'text-rose-400 bg-rose-500/10 border border-rose-500/20'
                              }`}
                            >
                              {isLong ? <ArrowUpRight className="w-3.5 h-3.5" /> : <ArrowDownRight className="w-3.5 h-3.5" />}
                              {isLong ? 'LONG' : 'SHORT'}
                            </span>
                          </td>

                          <td className="py-3.5 px-4 text-slate-200 font-bold">
                            {pos.quantity}
                            {pos.lot_size > 1 && (
                              <span className="text-[11px] text-slate-400 font-normal ml-1">
                                ({Math.round(Math.abs(pos.quantity) / pos.lot_size)} lots)
                              </span>
                            )}
                          </td>

                          <td className="py-3.5 px-4 text-white font-bold">
                            ₹{pos.average_price?.toFixed(2)}
                          </td>

                          <td className="py-3.5 px-4 text-cyan-300 font-extrabold">
                            ₹{pos.last_price?.toFixed(2)}
                          </td>

                          <td
                            className={`py-3.5 px-4 text-right font-extrabold ${
                              uPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'
                            }`}
                          >
                            {formatINR(uPnl)}
                          </td>

                          <td
                            className={`py-3.5 px-4 text-right font-bold ${
                              rPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'
                            }`}
                          >
                            {formatINR(rPnl)}
                          </td>

                          <td className="py-3.5 px-4 text-rose-400 font-bold">
                            {pos.strategy_stop_loss ? `₹${pos.strategy_stop_loss.toFixed(2)}` : '—'}
                          </td>

                          <td className="py-3.5 px-4 text-emerald-400 font-bold">
                            {pos.strategy_target ? `₹${pos.strategy_target.toFixed(2)}` : '—'}
                          </td>

                          <td className="py-3.5 px-4 text-center">
                            <button
                              onClick={() => handleSquareOff(pos.tradingsymbol)}
                              disabled={squaringOffSymbol === pos.tradingsymbol}
                              className="px-2.5 py-1 rounded text-xs font-bold bg-rose-500/10 text-rose-400 hover:bg-rose-500 hover:text-white border border-rose-500/30 transition-all disabled:opacity-50"
                            >
                              {squaringOffSymbol === pos.tradingsymbol ? 'Closing...' : 'Square Off'}
                            </button>
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
      )}

      {/* SUBTAB 2: HISTORICAL STRATEGY TRADE JOURNAL */}
      {activeSubTab === 'journal' && (
        <div className="space-y-4">
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <div className="text-xs font-mono text-slate-400">Filter Trades:</div>
              {['ALL', 'WINNERS', 'LOSERS'].map((f) => (
                <button
                  key={f}
                  onClick={() => setJournalFilter(f)}
                  className={`px-3 py-1 rounded text-xs font-bold font-mono transition-all ${
                    journalFilter === f
                      ? 'bg-indigo-600 text-white'
                      : 'text-slate-400 hover:text-white bg-white/5'
                  }`}
                >
                  {f}
                </button>
              ))}
            </div>
            <span className="text-xs font-mono text-slate-400">
              Showing {filteredTrades.length} of {trades.length} recorded trades
            </span>
          </div>

          <div className="term-panel overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-left font-mono text-xs sm:text-sm">
                <thead className="text-[11px] text-slate-400 uppercase bg-white/[0.02] border-b border-white/[0.08] select-none">
                  <tr>
                    <th className="py-3 px-4 font-semibold">Trade ID</th>
                    <th className="py-3 px-4 font-semibold">Symbol</th>
                    <th className="py-3 px-4 font-semibold">Side</th>
                    <th className="py-3 px-4 font-semibold">Qty</th>
                    <th className="py-3 px-4 font-semibold">Entry Price</th>
                    <th className="py-3 px-4 font-semibold">Exit Price</th>
                    <th className="py-3 px-4 font-semibold">Exit Reason</th>
                    <th className="py-3 px-4 font-semibold text-right">Net P&L</th>
                    <th className="py-3 px-4 font-semibold text-right">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-white/[0.04] text-slate-200">
                  {filteredTrades.length === 0 ? (
                    <tr>
                      <td colSpan={9} className="py-10 text-center text-sm text-slate-400 font-mono">
                        No trade history records matching filter.
                      </td>
                    </tr>
                  ) : (
                    filteredTrades.map((t) => {
                      const isLong = t.direction === 'BUY';
                      const pnl = t.pnl_net || 0;
                      const isClosed = Boolean(t.exit_price);

                      return (
                        <tr key={t.trade_id} className="hover:bg-white/[0.03] transition-colors">
                          <td className="py-3 px-4 text-slate-400 font-mono text-xs">
                            {t.trade_id}
                          </td>
                          <td className="py-3 px-4 font-bold text-white">{t.symbol}</td>
                          <td className="py-3 px-4">
                            <span
                              className={`px-2 py-0.5 rounded text-[11px] font-bold ${
                                isLong
                                  ? 'text-emerald-400 bg-emerald-500/10'
                                  : 'text-rose-400 bg-rose-500/10'
                              }`}
                            >
                              {t.direction}
                            </span>
                          </td>
                          <td className="py-3 px-4 text-slate-300">{t.quantity}</td>
                          <td className="py-3 px-4 font-bold text-white">
                            ₹{t.entry_price?.toFixed(2)}
                          </td>
                          <td className="py-3 px-4 text-slate-300">
                            {t.exit_price ? `₹${t.exit_price.toFixed(2)}` : '—'}
                          </td>
                          <td className="py-3 px-4 text-slate-400 text-xs">
                            {t.exit_reason || 'IN PROGRESS'}
                          </td>
                          <td
                            className={`py-3 px-4 text-right font-extrabold ${
                              pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'
                            }`}
                          >
                            {formatINR(pnl)}
                          </td>
                          <td className="py-3 px-4 text-right">
                            <span
                              className={`px-2 py-0.5 rounded text-[11px] font-bold ${
                                isClosed
                                  ? 'bg-white/5 text-slate-400'
                                  : 'bg-indigo-500/10 text-indigo-300 border border-indigo-500/20'
                              }`}
                            >
                              {isClosed ? 'CLOSED' : 'OPEN'}
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
      )}
    </div>
  );
}
