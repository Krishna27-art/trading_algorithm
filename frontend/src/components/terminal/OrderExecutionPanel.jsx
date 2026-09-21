import React, { useState } from 'react';
import { AlertTriangle, ShieldCheck, Check, X, ShieldAlert, ArrowRight } from 'lucide-react';

export default function OrderExecutionPanel({ tradingMode, setTradingMode, onOrderPlaced, ltp }) {
  const [direction, setDirection] = useState('BUY');
  const [quantity, setQuantity] = useState(25);
  const [orderType, setOrderType] = useState('LIMIT');
  const [limitPrice, setLimitPrice] = useState(ltp ? ltp.toFixed(2) : '24150.00');
  const [showConfirmModal, setShowConfirmModal] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const isLive = tradingMode === 'LIVE';

  // Calculated estimates
  const estPrice = parseFloat(limitPrice) || ltp || 24000;
  const turnover = estPrice * quantity;
  const estRisk = 50 * quantity; // assuming 50 pt stop
  const estCharges = Math.round(40 + (turnover * 0.0002) + (turnover * 0.000019) + 8); // Brokerage + STT + Txn + GST

  const handleOpenConfirm = (e) => {
    e.preventDefault();
    setShowConfirmModal(true);
  };

  const handleExecuteOrder = async () => {
    setIsSubmitting(true);
    try {
      const res = await fetch('/api/orders/place', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Shared-Secret': 'trading-algo-dev-secret-key',
        },
        body: JSON.stringify({
          symbol: 'NIFTY',
          direction: direction,
          order_type: orderType,
          price: parseFloat(limitPrice),
          quantity: parseInt(quantity),
          mode: tradingMode,
        }),
      });
      const data = await res.json();
      if (data.success) {
        setShowConfirmModal(false);
        if (onOrderPlaced) onOrderPlaced(data.order);
      } else {
        alert(data.detail || 'Order failed');
      }
    } catch (e) {
      alert('Error routing order: ' + e.message);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className={`term-panel p-3.5 space-y-3 ${isLive ? 'term-panel-live' : ''}`}>
      {/* Header & Mode Switcher */}
      <div className="flex items-center justify-between pb-2 border-b border-white/[0.06]">
        <div className="flex items-center gap-2">
          <h3 className="text-xs font-bold text-slate-100 uppercase tracking-wider">Order Execution</h3>
          <span className={`term-badge text-[10px] font-mono ${isLive ? 'term-badge-amber' : 'term-badge-emerald'}`}>
            {isLive ? 'LIVE BROKER GATEWAY' : 'PAPER SIMULATOR'}
          </span>
        </div>

        <div className="flex items-center text-[10px] font-mono font-semibold bg-black/40 rounded p-0.5 border border-white/5">
          <button
            type="button"
            onClick={() => setTradingMode('PAPER')}
            className={`px-2 py-0.5 rounded ${!isLive ? 'bg-emerald-500/20 text-emerald-300' : 'text-slate-400'}`}
          >
            PAPER
          </button>
          <button
            type="button"
            onClick={() => setTradingMode('LIVE')}
            className={`px-2 py-0.5 rounded ${isLive ? 'bg-amber-500/20 text-amber-300' : 'text-slate-400'}`}
          >
            LIVE
          </button>
        </div>
      </div>

      {/* Live Warning Banner */}
      {isLive && (
        <div className="p-2 rounded bg-amber-500/10 border border-amber-500/30 text-[11px] text-amber-300 flex items-center gap-2 font-mono">
          <AlertTriangle className="w-4 h-4 flex-shrink-0 text-amber-400" />
          <span>CAUTION: Orders placed will route to Zerodha Kite with real capital.</span>
        </div>
      )}

      {/* Quick Order Form */}
      <form onSubmit={handleOpenConfirm} className="space-y-2.5 text-xs font-mono">
        {/* Buy / Sell Buttons */}
        <div className="grid grid-cols-2 gap-2">
          <button
            type="button"
            onClick={() => setDirection('BUY')}
            className={`py-1.5 rounded font-bold transition-all border ${
              direction === 'BUY'
                ? 'bg-emerald-500 text-white border-emerald-400 shadow-sm shadow-emerald-500/20'
                : 'bg-white/[0.03] text-slate-400 border-white/5 hover:text-white'
            }`}
          >
            BUY / LONG
          </button>
          <button
            type="button"
            onClick={() => setDirection('SELL')}
            className={`py-1.5 rounded font-bold transition-all border ${
              direction === 'SELL'
                ? 'bg-rose-500 text-white border-rose-400 shadow-sm shadow-rose-500/20'
                : 'bg-white/[0.03] text-slate-400 border-white/5 hover:text-white'
            }`}
          >
            SELL / SHORT
          </button>
        </div>

        {/* Quantity & Order Type */}
        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="text-[10px] text-slate-400 block mb-1">Quantity (Lots: 25)</label>
            <input
              type="number"
              step="25"
              min="25"
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
              className="term-input py-1"
            />
          </div>
          <div>
            <label className="text-[10px] text-slate-400 block mb-1">Order Type</label>
            <select
              value={orderType}
              onChange={(e) => setOrderType(e.target.value)}
              className="term-input py-1 cursor-pointer"
            >
              <option value="LIMIT">LIMIT (Marketable)</option>
              <option value="MARKET">MARKET</option>
            </select>
          </div>
        </div>

        {/* Limit Price */}
        {orderType === 'LIMIT' && (
          <div>
            <label className="text-[10px] text-slate-400 block mb-1">Limit Price (₹)</label>
            <input
              type="number"
              step="0.05"
              value={limitPrice}
              onChange={(e) => setLimitPrice(e.target.value)}
              className="term-input py-1"
            />
          </div>
        )}

        {/* Submit Button */}
        <button
          type="submit"
          className={`w-full py-2 rounded font-bold tracking-wider text-xs ${
            isLive ? 'term-btn-rose' : direction === 'BUY' ? 'term-btn-emerald' : 'term-btn-rose'
          }`}
        >
          {isLive ? 'PROCEED TO CONFIRM LIVE ORDER' : `PLACE ${direction} (PAPER)`}
        </button>
      </form>

      {/* Modal Confirmation for Safety (Explicit Requirement) */}
      {showConfirmModal && (
        <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="w-full max-w-md term-panel p-5 space-y-4 border-amber-500/40 shadow-2xl animate-fade-in">
            <div className="flex items-center justify-between pb-3 border-b border-white/10">
              <div className="flex items-center gap-2 text-amber-400">
                <ShieldAlert className="w-5 h-5" />
                <h3 className="text-sm font-bold uppercase tracking-wider text-white">
                  Confirm {tradingMode} Execution
                </h3>
              </div>
              <button
                onClick={() => setShowConfirmModal(false)}
                className="p-1 rounded text-slate-400 hover:text-white"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="space-y-2 text-xs font-mono">
              <div className="flex justify-between py-1 border-b border-white/5">
                <span className="text-slate-400">Instrument:</span>
                <span className="text-white font-bold">NIFTY 50 Futures (MIS)</span>
              </div>
              <div className="flex justify-between py-1 border-b border-white/5">
                <span className="text-slate-400">Action:</span>
                <span className={`font-bold ${direction === 'BUY' ? 'text-emerald-400' : 'text-rose-400'}`}>
                  {direction}
                </span>
              </div>
              <div className="flex justify-between py-1 border-b border-white/5">
                <span className="text-slate-400">Quantity:</span>
                <span className="text-white">{quantity} units (1 lot)</span>
              </div>
              <div className="flex justify-between py-1 border-b border-white/5">
                <span className="text-slate-400">Order Price:</span>
                <span className="text-white">₹{parseFloat(limitPrice).toFixed(2)} ({orderType})</span>
              </div>
              <div className="flex justify-between py-1 border-b border-white/5">
                <span className="text-slate-400">Max Trade Risk (1%):</span>
                <span className="text-rose-400 font-bold">₹{estRisk.toLocaleString('en-IN')}</span>
              </div>
              <div className="flex justify-between py-1 border-b border-white/5">
                <span className="text-slate-400">Est. Statutory Taxes:</span>
                <span className="text-slate-300">₹{estCharges} (STT, Brokerage, GST)</span>
              </div>
            </div>

            {isLive && (
              <div className="p-3 rounded bg-rose-500/10 border border-rose-500/30 text-xs text-rose-300 leading-snug">
                <strong>WARNING:</strong> This order will be routed to your live Zerodha account. Are you sure you want to proceed?
              </div>
            )}

            <div className="flex items-center gap-3 pt-2">
              <button
                type="button"
                onClick={() => setShowConfirmModal(false)}
                className="flex-1 term-btn-secondary py-2"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleExecuteOrder}
                disabled={isSubmitting}
                className={`flex-1 py-2 font-bold font-mono text-xs rounded text-white ${
                  isLive ? 'bg-amber-600 hover:bg-amber-500' : 'bg-emerald-600 hover:bg-emerald-500'
                }`}
              >
                {isSubmitting ? 'Submitting...' : 'CONFIRM ORDER'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
