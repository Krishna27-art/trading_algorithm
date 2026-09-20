import React from 'react';
import { FileText, RefreshCw, Clock, CheckCircle2, XCircle, AlertCircle } from 'lucide-react';

export default function OrdersPage({ orders, onRefresh }) {
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
      return d.toLocaleTimeString('en-GB', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        timeZone: 'Asia/Kolkata',
      });
    } catch {
      return timeStr.slice(11, 19) || timeStr;
    }
  };

  return (
    <div className="space-y-4">
      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-white/[0.08]">
        <div>
          <h2 className="text-xl font-extrabold text-white tracking-tight flex items-center gap-2.5">
            <FileText className="w-5 h-5 text-indigo-400" /> Order History & Audit Trail
          </h2>
          <p className="text-xs text-slate-400 font-mono mt-0.5">
            Timestamped execution log from Zerodha Kite broker router
          </p>
        </div>

        <button
          onClick={onRefresh}
          className="p-2 rounded-lg text-slate-300 hover:text-white hover:bg-white/10 border border-white/5 transition-colors flex items-center gap-2 text-xs font-mono font-bold"
        >
          <RefreshCw className="w-4 h-4" /> Refresh Orders
        </button>
      </div>

      {/* Full Width Table */}
      <div className="term-panel overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-left font-mono text-xs sm:text-sm">
            <thead className="text-xs text-slate-400 uppercase bg-white/[0.02] border-b border-white/[0.08] select-none">
              <tr>
                <th className="py-3 px-4 font-semibold">Time (IST)</th>
                <th className="py-3 px-4 font-semibold">Instrument</th>
                <th className="py-3 px-4 font-semibold">Side</th>
                <th className="py-3 px-4 font-semibold">Quantity</th>
                <th className="py-3 px-4 font-semibold">Order Price</th>
                <th className="py-3 px-4 font-semibold">Order Type</th>
                <th className="py-3 px-4 font-semibold text-right">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/[0.04] text-slate-200">
              {(!orders || orders.length === 0) ? (
                <tr>
                  <td colSpan={7} className="py-12 text-center text-sm text-slate-400 font-mono">
                    No orders logged for the current trading session.
                  </td>
                </tr>
              ) : (
                orders.map((ord, idx) => {
                  const isBuy = ord.direction === 'BUY';

                  return (
                    <tr key={ord.order_id || idx} className="hover:bg-white/[0.03] transition-colors">
                      <td className="py-3.5 px-4 text-slate-300">{formatTime(ord.created_at)}</td>
                      <td className="py-3.5 px-4 font-extrabold text-white">{ord.symbol}</td>
                      <td className="py-3.5 px-4">
                        <span
                          className={`px-2.5 py-1 rounded text-xs font-bold ${
                            isBuy
                              ? 'text-emerald-400 bg-emerald-500/10 border border-emerald-500/20'
                              : 'text-rose-400 bg-rose-500/10 border border-rose-500/20'
                          }`}
                        >
                          {ord.direction}
                        </span>
                      </td>
                      <td className="py-3.5 px-4 text-slate-300 font-semibold">{ord.quantity}</td>
                      <td className="py-3.5 px-4 font-bold text-white">
                        ₹{(ord.average_fill_price || ord.price || 0).toFixed(2)}
                      </td>
                      <td className="py-3.5 px-4 text-slate-400 font-semibold">{ord.order_type || 'MARKET'}</td>
                      <td className="py-3.5 px-4 text-right">
                        <span className={`term-badge text-xs font-bold ${getStatusBadge(ord.status)}`}>
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
    </div>
  );
}
