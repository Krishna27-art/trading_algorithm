import React, { useState, useEffect } from 'react';
import {
  X,
  Zap,
  ArrowUpRight,
  ArrowDownRight,
  ShieldAlert,
  AlertTriangle,
  CheckCircle2,
} from 'lucide-react';

export default function QuickOrderModal({
  isOpen,
  onClose,
  initialData,
  onOrderSuccess,
  tradingMode = 'PAPER',
  setTradingMode,
}) {
  const [symbol, setSymbol] = useState('');
  const [direction, setDirection] = useState('BUY');
  const [orderType, setOrderType] = useState('LIMIT');
  const [price, setPrice] = useState('');
  const [stopLoss, setStopLoss] = useState('');
  const [target, setTarget] = useState('');
  const [quantity, setQuantity] = useState(1);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState(null);
  const [successMsg, setSuccessMsg] = useState(null);

  useEffect(() => {
    if (initialData) {
      setSymbol(initialData.symbol || '');
      setDirection(initialData.direction?.toUpperCase() === 'SHORT' ? 'SELL' : 'BUY');
      setPrice(initialData.entry ? String(initialData.entry) : (initialData.ltp ? String(initialData.ltp) : ''));
      setStopLoss(initialData.stop_loss ? String(initialData.stop_loss) : '');
      setTarget(initialData.target ? String(initialData.target) : '');
      setQuantity(initialData.quantity || (initialData.symbol === 'NIFTY' ? 65 : 1));
      setErrorMsg(null);
      setSuccessMsg(null);
    }
  }, [initialData]);

  if (!isOpen) return null;

  const isBuy = direction === 'BUY';

  const handleSubmit = async (e) => {
    e.preventDefault();
    setErrorMsg(null);
    setSuccessMsg(null);

    // Form Validations
    if (!symbol.trim()) {
      setErrorMsg('Symbol is required.');
      return;
    }
    const numQty = parseInt(quantity, 10);
    if (isNaN(numQty) || numQty <= 0) {
      setErrorMsg('Quantity must be a positive integer.');
      return;
    }
    const numPrice = parseFloat(price);
    if (orderType === 'LIMIT' && (isNaN(numPrice) || numPrice <= 0)) {
      setErrorMsg('Limit price must be greater than 0.');
      return;
    }

    if (tradingMode === 'LIVE') {
      const confirmMsg = `WARNING: You are about to place a REAL LIVE ${direction} order on Zerodha Kite for ${numQty} shares of ${symbol} at ₹${numPrice || 'MARKET'}. Proceed?`;
      if (!window.confirm(confirmMsg)) return;
    }

    setIsSubmitting(true);
    try {
      const res = await fetch('/api/orders/place', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Shared-Secret': localStorage.getItem('app_shared_secret') || '',
        },
        body: JSON.stringify({
          symbol: symbol.toUpperCase(),
          direction: direction,
          order_type: orderType,
          price: orderType === 'LIMIT' ? numPrice : null,
          quantity: numQty,
          mode: tradingMode,
        }),
      });

      const data = await res.json();
      if (res.ok && data.success) {
        setSuccessMsg(
          `Order executed successfully! Order ID: ${data.order?.order_id || 'OK'} (${tradingMode} Mode)`
        );
        if (onOrderSuccess) {
          setTimeout(() => {
            onOrderSuccess();
            onClose();
          }, 1200);
        }
      } else {
        const err = data.detail || data.message || 'Order was rejected by broker or pre-trade risk gates.';
        setErrorMsg(err);
      }
    } catch (err) {
      setErrorMsg(`Network failure placing order: ${err.message}`);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm animate-fade-in">
      <div className="w-full max-w-lg rounded-2xl bg-[#0e1422] border border-white/10 shadow-2xl overflow-hidden font-sans">
        {/* Header */}
        <div className="px-6 py-4 bg-white/[0.02] border-b border-white/10 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <div
              className={`p-2 rounded-lg ${
                isBuy ? 'bg-emerald-500/20 text-emerald-400' : 'bg-rose-500/20 text-rose-400'
              }`}
            >
              {isBuy ? <ArrowUpRight className="w-5 h-5" /> : <ArrowDownRight className="w-5 h-5" />}
            </div>
            <div>
              <h3 className="text-base font-bold text-white font-mono tracking-wide">
                Order Execution: {symbol || 'Select Instrument'}
              </h3>
              <p className="text-xs text-slate-400">
                Direct order placement through institutional pre-trade risk gates
              </p>
            </div>
          </div>

          <button
            onClick={onClose}
            className="p-1.5 rounded-lg hover:bg-white/10 text-slate-400 hover:text-white transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content Form */}
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          {/* Mode Switcher */}
          <div className="p-3 rounded-xl bg-black/40 border border-white/5 flex items-center justify-between">
            <div>
              <span className="text-xs font-mono font-bold text-white block">Trading Mode</span>
              <span className="text-[11px] text-slate-400">
                {tradingMode === 'LIVE'
                  ? 'Real orders routed to Zerodha Kite Connect'
                  : 'Zero-risk simulator using real Kite price feeds'}
              </span>
            </div>

            <div className="flex bg-black/60 rounded-lg p-1 border border-white/10">
              <button
                type="button"
                onClick={() => setTradingMode && setTradingMode('PAPER')}
                className={`px-3 py-1 rounded text-xs font-mono font-bold transition-all ${
                  tradingMode === 'PAPER'
                    ? 'bg-indigo-600 text-white shadow'
                    : 'text-slate-400 hover:text-white'
                }`}
              >
                PAPER
              </button>
              <button
                type="button"
                onClick={() => setTradingMode && setTradingMode('LIVE')}
                className={`px-3 py-1 rounded text-xs font-mono font-bold transition-all ${
                  tradingMode === 'LIVE'
                    ? 'bg-rose-600 text-white shadow'
                    : 'text-slate-400 hover:text-white'
                }`}
              >
                LIVE
              </button>
            </div>
          </div>

          {tradingMode === 'LIVE' && (
            <div className="p-3 rounded-lg bg-rose-500/10 border border-rose-500/30 text-xs text-rose-300 flex items-center gap-2">
              <ShieldAlert className="w-4 h-4 flex-shrink-0 text-rose-400" />
              <span>
                <strong>LIVE TRADING ACTIVE:</strong> Capital at risk. Verify orders before submitting.
              </span>
            </div>
          )}

          {/* Direction Toggle */}
          <div className="grid grid-cols-2 gap-3 font-mono">
            <button
              type="button"
              onClick={() => setDirection('BUY')}
              className={`py-2.5 rounded-xl border text-sm font-bold flex items-center justify-center gap-2 transition-all ${
                isBuy
                  ? 'bg-emerald-500/20 border-emerald-500 text-emerald-300 shadow-md shadow-emerald-500/10'
                  : 'bg-white/[0.02] border-white/10 text-slate-400 hover:bg-white/[0.05]'
              }`}
            >
              <ArrowUpRight className="w-4 h-4" />
              BUY / LONG
            </button>

            <button
              type="button"
              onClick={() => setDirection('SELL')}
              className={`py-2.5 rounded-xl border text-sm font-bold flex items-center justify-center gap-2 transition-all ${
                !isBuy
                  ? 'bg-rose-500/20 border-rose-500 text-rose-300 shadow-md shadow-rose-500/10'
                  : 'bg-white/[0.02] border-white/10 text-slate-400 hover:bg-white/[0.05]'
              }`}
            >
              <ArrowDownRight className="w-4 h-4" />
              SELL / SHORT
            </button>
          </div>

          {/* Inputs Grid */}
          <div className="grid grid-cols-2 gap-3 text-xs font-mono">
            <div>
              <label className="text-slate-400 block mb-1">Instrument Symbol</label>
              <input
                type="text"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                className="w-full bg-black/40 border border-white/10 rounded-lg px-3 py-2 text-white font-bold"
                required
              />
            </div>

            <div>
              <label className="text-slate-400 block mb-1">Quantity (Units / Lots)</label>
              <input
                type="number"
                min="1"
                value={quantity}
                onChange={(e) => setQuantity(e.target.value)}
                className="w-full bg-black/40 border border-white/10 rounded-lg px-3 py-2 text-white font-bold"
                required
              />
            </div>

            <div>
              <label className="text-slate-400 block mb-1">Order Type</label>
              <select
                value={orderType}
                onChange={(e) => setOrderType(e.target.value)}
                className="w-full bg-black/40 border border-white/10 rounded-lg px-3 py-2 text-white font-semibold"
              >
                <option value="LIMIT">LIMIT ORDER</option>
                <option value="MARKET">MARKET ORDER</option>
              </select>
            </div>

            <div>
              <label className="text-slate-400 block mb-1">Entry Price (₹)</label>
              <input
                type="number"
                step="0.05"
                value={price}
                onChange={(e) => setPrice(e.target.value)}
                disabled={orderType === 'MARKET'}
                placeholder={orderType === 'MARKET' ? 'Market Execution' : '0.00'}
                className="w-full bg-black/40 border border-white/10 rounded-lg px-3 py-2 text-white font-bold disabled:opacity-50"
              />
            </div>

            <div>
              <label className="text-slate-400 block mb-1">Stop Loss (₹)</label>
              <input
                type="number"
                step="0.05"
                value={stopLoss}
                onChange={(e) => setStopLoss(e.target.value)}
                placeholder="Optional"
                className="w-full bg-black/40 border border-white/10 rounded-lg px-3 py-2 text-rose-400 font-bold"
              />
            </div>

            <div>
              <label className="text-slate-400 block mb-1">Target (₹)</label>
              <input
                type="number"
                step="0.05"
                value={target}
                onChange={(e) => setTarget(e.target.value)}
                placeholder="Optional"
                className="w-full bg-black/40 border border-white/10 rounded-lg px-3 py-2 text-emerald-400 font-bold"
              />
            </div>
          </div>

          {/* Feedback messages */}
          {errorMsg && (
            <div className="p-3 rounded-lg bg-rose-500/15 border border-rose-500/30 text-xs text-rose-300 flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 flex-shrink-0 text-rose-400 mt-0.5" />
              <span>{errorMsg}</span>
            </div>
          )}

          {successMsg && (
            <div className="p-3 rounded-lg bg-emerald-500/15 border border-emerald-500/30 text-xs text-emerald-300 flex items-start gap-2">
              <CheckCircle2 className="w-4 h-4 flex-shrink-0 text-emerald-400 mt-0.5" />
              <span>{successMsg}</span>
            </div>
          )}

          {/* Submit Button */}
          <button
            type="submit"
            disabled={isSubmitting}
            className={`w-full py-3 rounded-xl font-mono font-bold text-sm tracking-wider shadow-xl transition-all flex items-center justify-center gap-2 ${
              isBuy
                ? 'bg-emerald-500 hover:bg-emerald-400 text-slate-950 shadow-emerald-500/20'
                : 'bg-rose-500 hover:bg-rose-400 text-white shadow-rose-500/20'
            } disabled:opacity-50`}
          >
            <Zap className="w-4 h-4" />
            <span>
              {isSubmitting
                ? 'TRANSMITTING ORDER...'
                : `SUBMIT ${direction} ORDER (${tradingMode} MODE)`}
            </span>
          </button>
        </form>
      </div>
    </div>
  );
}
