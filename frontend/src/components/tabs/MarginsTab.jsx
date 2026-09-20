import React, { useEffect, useState } from 'react';
import { Wallet, TrendingUp, DollarSign, RefreshCw, AlertTriangle, ArrowUpRight } from 'lucide-react';

export default function MarginsTab() {
  const [margins, setMargins] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const fetchMargins = async () => {
    setLoading(true);
    setError('');
    try {
      const res = await fetch('/api/margins');
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Failed to fetch margins');
      }
      const data = await res.json();
      setMargins(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchMargins();
  }, []);

  const formatINR = (val) => {
    if (val === undefined || val === null) return '₹0.00';
    return new Intl.NumberFormat('en-IN', {
      style: 'currency',
      currency: 'INR',
      maximumFractionDigits: 2,
    }).format(val);
  };

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center py-20 gap-4 text-slate-400">
        <RefreshCw className="w-8 h-8 animate-spin text-emerald-400" />
        <p className="text-sm font-medium">Fetching live account margins & funds...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="glass-panel p-6 border-rose-500/30 text-rose-300 flex items-start gap-4">
        <AlertTriangle className="w-6 h-6 text-rose-400 flex-shrink-0" />
        <div>
          <h3 className="font-semibold text-rose-200">Unable to load margins</h3>
          <p className="text-sm text-slate-300 mt-1">{error}</p>
          <button onClick={fetchMargins} className="mt-4 btn-secondary text-xs">
            Retry
          </button>
        </div>
      </div>
    );
  }

  const equity = margins?.equity || {};
  const commodity = margins?.commodity || {};

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Overview Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5">
        {/* Equity Net Balance */}
        <div className="glass-panel p-5 relative overflow-hidden border-emerald-500/20">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Equity Net Available</span>
            <div className="p-2 rounded-lg bg-emerald-500/10 text-emerald-400">
              <Wallet className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3">
            <h3 className="text-2xl font-bold font-mono text-white">{formatINR(equity.net || 0)}</h3>
            <p className="text-xs text-slate-400 mt-1">Available for live trading & orders</p>
          </div>
        </div>

        {/* Equity Utilized Margin */}
        <div className="glass-panel p-5 relative overflow-hidden border-indigo-500/20">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Equity Utilized Margin</span>
            <div className="p-2 rounded-lg bg-indigo-500/10 text-indigo-400">
              <TrendingUp className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3">
            <h3 className="text-2xl font-bold font-mono text-white">{formatINR(equity.utilised?.debits || 0)}</h3>
            <p className="text-xs text-slate-400 mt-1">Total debits / position margins locked</p>
          </div>
        </div>

        {/* Commodity Available */}
        <div className="glass-panel p-5 relative overflow-hidden border-amber-500/20 sm:col-span-2 lg:col-span-1">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Commodity Available</span>
            <div className="p-2 rounded-lg bg-amber-500/10 text-amber-400">
              <DollarSign className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3">
            <h3 className="text-2xl font-bold font-mono text-white">{formatINR(commodity.net || 0)}</h3>
            <p className="text-xs text-slate-400 mt-1">MCX Commodity segment balance</p>
          </div>
        </div>
      </div>

      {/* Breakdown Details */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Equity Breakdown */}
        <div className="glass-panel p-6 space-y-4">
          <div className="flex items-center justify-between pb-3 border-b border-white/5">
            <h3 className="text-base font-bold text-white flex items-center gap-2">
              <span className="w-2.5 h-2.5 rounded-full bg-emerald-400"></span>
              Equity Funds Breakdown
            </h3>
            <span className="text-xs font-mono text-slate-400">INR</span>
          </div>

          <div className="space-y-3 font-mono text-sm">
            <div className="flex items-center justify-between py-1.5 border-b border-white/5">
              <span className="text-slate-400 font-sans text-xs">Opening Balance:</span>
              <span className="text-slate-200">{formatINR(equity.available?.opening_balance || 0)}</span>
            </div>
            <div className="flex items-center justify-between py-1.5 border-b border-white/5">
              <span className="text-slate-400 font-sans text-xs">Live Cash Balance:</span>
              <span className="text-slate-200">{formatINR(equity.available?.live_balance || 0)}</span>
            </div>
            <div className="flex items-center justify-between py-1.5 border-b border-white/5">
              <span className="text-slate-400 font-sans text-xs">Collateral Margin:</span>
              <span className="text-slate-200">{formatINR(equity.available?.collateral || 0)}</span>
            </div>
            <div className="flex items-center justify-between py-1.5 border-b border-white/5">
              <span className="text-slate-400 font-sans text-xs">Span Margin (Locked):</span>
              <span className="text-rose-300">{formatINR(equity.utilised?.span || 0)}</span>
            </div>
            <div className="flex items-center justify-between py-1.5">
              <span className="text-slate-400 font-sans text-xs">Exposure Margin:</span>
              <span className="text-rose-300">{formatINR(equity.utilised?.exposure || 0)}</span>
            </div>
          </div>
        </div>

        {/* Commodity Breakdown */}
        <div className="glass-panel p-6 space-y-4">
          <div className="flex items-center justify-between pb-3 border-b border-white/5">
            <h3 className="text-base font-bold text-white flex items-center gap-2">
              <span className="w-2.5 h-2.5 rounded-full bg-amber-400"></span>
              Commodity Funds Breakdown
            </h3>
            <span className="text-xs font-mono text-slate-400">INR</span>
          </div>

          <div className="space-y-3 font-mono text-sm">
            <div className="flex items-center justify-between py-1.5 border-b border-white/5">
              <span className="text-slate-400 font-sans text-xs">Opening Balance:</span>
              <span className="text-slate-200">{formatINR(commodity.available?.opening_balance || 0)}</span>
            </div>
            <div className="flex items-center justify-between py-1.5 border-b border-white/5">
              <span className="text-slate-400 font-sans text-xs">Live Cash Balance:</span>
              <span className="text-slate-200">{formatINR(commodity.available?.live_balance || 0)}</span>
            </div>
            <div className="flex items-center justify-between py-1.5 border-b border-white/5">
              <span className="text-slate-400 font-sans text-xs">Collateral Margin:</span>
              <span className="text-slate-200">{formatINR(commodity.available?.collateral || 0)}</span>
            </div>
            <div className="flex items-center justify-between py-1.5 border-b border-white/5">
              <span className="text-slate-400 font-sans text-xs">Span Margin (Locked):</span>
              <span className="text-rose-300">{formatINR(commodity.utilised?.span || 0)}</span>
            </div>
            <div className="flex items-center justify-between py-1.5">
              <span className="text-slate-400 font-sans text-xs">Exposure Margin:</span>
              <span className="text-rose-300">{formatINR(commodity.utilised?.exposure || 0)}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
