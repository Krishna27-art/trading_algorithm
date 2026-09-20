import React from 'react';
import { Clock, CheckCircle2, XCircle, AlertCircle, RefreshCw } from 'lucide-react';

export default function OrderHistoryTable({ orders, onRefresh }) {
  const getStatusBadge = (status) => {
    switch (status) {
      case 'COMPLETE':
      case 'FILLED':
        return 'term-badge-emerald';
      case 'OPEN':
      case 'SUBMITTED':
      case 'PENDING':
        return 'term-badge-indigo';
      case 'CANCELLED':
        return 'term-badge-neutral';
      case 'REJECTED':
        return 'term-badge-rose';
      default:
        return 'term-badge-neutral';
    }
  };

  const formatTime = (timeStr) => {
    if (!timeStr) return '--:--';
    try {
      const d = new Date(timeStr);
      return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch {
      return timeStr.slice(11, 19) || timeStr;
    }
  };

  return (
    <div className="term-panel p-3.5 space-y-3">
      {/* Header */}
      <div className="flex items-center justify-between pb-2 border-b border-white/[0.06]">
        <div className="flex items-center gap-2">
          <Clock className="w-4 h-4 text-cyan-400" />
          <h3 className="text-xs font-bold text-slate-100 uppercase tracking-wider">Order History</h3>
          <span className="text-[10px] text-slate-400 font-mono">({orders.length})</span>
        </div>

        <button
          onClick={onRefresh}
          className="p-1 rounded text-slate-400 hover:text-white"
          title="Refresh orders"
        >
          <RefreshCw className="w-3 h-3" />
        </button>
      </div>

      {/* Orders List / Table */}
      <div className="overflow-x-auto max-h-[200px] overflow-y-auto custom-scrollbar">
        <table className="w-full text-left font-mono text-[11px]">
          <thead className="text-[10px] text-slate-400 uppercase border-b border-white/5 pb-1 select-none">
            <tr>
              <th className="py-1.5 font-semibold">Time</th>
              <th className="font-semibold">Instrument</th>
              <th className="font-semibold">Action</th>
              <th className="font-semibold">Qty</th>
              <th className="font-semibold">Price</th>
              <th className="font-semibold">Type</th>
              <th className="font-semibold text-right pr-1">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/[0.03] text-slate-200">
            {orders.length === 0 ? (
              <tr>
                <td colSpan={7} className="py-6 text-center text-xs text-slate-500 font-mono">
                  No order history records logged.
                </td>
              </tr>
            ) : (
              orders.map((ord, idx) => {
                const isBuy = ord.direction === 'BUY';
                return (
                  <tr key={ord.order_id || idx} className="hover:bg-white/[0.02] transition-colors">
                    <td className="py-2 text-slate-400">{formatTime(ord.created_at)}</td>
                    <td className="font-bold text-slate-100">{ord.symbol}</td>
                    <td>
                      <span className={`px-1.5 py-0.2 rounded text-[10px] font-bold ${
                        isBuy ? 'text-emerald-400 bg-emerald-500/10' : 'text-rose-400 bg-rose-500/10'
                      }`}>
                        {ord.direction}
                      </span>
                    </td>
                    <td>{ord.quantity}</td>
                    <td>₹{(ord.average_fill_price || ord.price || 0).toFixed(2)}</td>
                    <td className="text-slate-400">{ord.order_type}</td>
                    <td className="text-right pr-1">
                      <span className={`term-badge text-[9px] ${getStatusBadge(ord.status)}`}>
                        {ord.status}
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
