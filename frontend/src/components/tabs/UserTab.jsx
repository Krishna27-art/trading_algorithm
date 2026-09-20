import React, { useEffect, useState } from 'react';
import { User, Shield, CheckCircle2, Layers, Globe, Mail, Building2, RefreshCw, AlertTriangle, Key } from 'lucide-react';

export default function UserTab() {
  const [profile, setProfile] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const fetchProfile = async () => {
    setLoading(true);
    setError('');
    try {
      const res = await fetch('/api/profile');
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Failed to fetch user profile');
      }
      const data = await res.json();
      setProfile(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchProfile();
  }, []);

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center py-20 gap-4 text-slate-400">
        <RefreshCw className="w-8 h-8 animate-spin text-indigo-400" />
        <p className="text-sm font-medium">Fetching profile details from Zerodha Kite...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="glass-panel p-6 border-rose-500/30 text-rose-300 flex items-start gap-4">
        <AlertTriangle className="w-6 h-6 text-rose-400 flex-shrink-0" />
        <div>
          <h3 className="font-semibold text-rose-200">Unable to load profile</h3>
          <p className="text-sm text-slate-300 mt-1">{error}</p>
          <button
            onClick={fetchProfile}
            className="mt-4 btn-secondary text-xs"
          >
            Retry Fetch
          </button>
        </div>
      </div>
    );
  }

  const getProductColor = (product) => {
    switch (product) {
      case 'CNC': return 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30';
      case 'MIS': return 'bg-indigo-500/10 text-indigo-300 border-indigo-500/30';
      case 'NRML': return 'bg-cyan-500/10 text-cyan-300 border-cyan-500/30';
      case 'BO': return 'bg-amber-500/10 text-amber-300 border-amber-500/30';
      case 'CO': return 'bg-purple-500/10 text-purple-300 border-purple-500/30';
      default: return 'bg-slate-500/10 text-slate-300 border-slate-500/30';
    }
  };

  const getExchangeColor = (exchange) => {
    switch (exchange) {
      case 'NSE': return 'bg-blue-500/10 text-blue-300 border-blue-500/30';
      case 'BSE': return 'bg-sky-500/10 text-sky-300 border-sky-500/30';
      case 'NFO': return 'bg-indigo-500/10 text-indigo-300 border-indigo-500/30';
      case 'MCX': return 'bg-amber-500/10 text-amber-300 border-amber-500/30';
      case 'CDS': return 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30';
      case 'BFO': return 'bg-violet-500/10 text-violet-300 border-violet-500/30';
      default: return 'bg-slate-500/10 text-slate-300 border-slate-500/30';
    }
  };

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Top Profile Card */}
      <div className="glass-panel p-6 relative overflow-hidden">
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-6">
          <div className="flex items-center gap-4">
            <div className="w-16 h-16 rounded-2xl bg-gradient-to-tr from-indigo-600 via-indigo-500 to-emerald-400 flex items-center justify-center text-white font-bold text-2xl shadow-xl shadow-indigo-500/20 ring-2 ring-white/10">
              {profile?.user_name ? profile.user_name.charAt(0).toUpperCase() : 'U'}
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-2xl font-extrabold text-white tracking-tight">{profile?.user_name || 'Trader'}</h2>
                <span className="badge badge-emerald">
                  <CheckCircle2 className="w-3.5 h-3.5" /> Active Session
                </span>
              </div>
              <div className="flex items-center gap-3 mt-1.5 text-xs text-slate-400 font-mono">
                <span className="flex items-center gap-1">
                  <Key className="w-3.5 h-3.5 text-indigo-400" />
                  ID: <span className="text-slate-200 font-semibold">{profile?.user_id}</span>
                </span>
                <span>•</span>
                <span className="flex items-center gap-1">
                  <Building2 className="w-3.5 h-3.5 text-emerald-400" />
                  Broker: <span className="text-slate-200">{profile?.broker || 'ZERODHA'}</span>
                </span>
                {profile?.email && (
                  <>
                    <span>•</span>
                    <span className="flex items-center gap-1 font-sans">
                      <Mail className="w-3.5 h-3.5 text-slate-400" />
                      {profile.email}
                    </span>
                  </>
                )}
              </div>
            </div>
          </div>

          <button
            onClick={fetchProfile}
            className="btn-secondary self-start md:self-auto text-xs"
            title="Refresh profile info"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            <span>Refresh Profile</span>
          </button>
        </div>
      </div>

      {/* Grid: Products & Exchanges */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Products Section */}
        <div className="glass-panel p-6 space-y-4">
          <div className="flex items-center justify-between pb-3 border-b border-white/5">
            <div className="flex items-center gap-2">
              <div className="p-2 rounded-lg bg-indigo-500/10 text-indigo-400">
                <Layers className="w-5 h-5" />
              </div>
              <div>
                <h3 className="text-base font-bold text-white">Enabled Products</h3>
                <p className="text-xs text-slate-400">Trading product types supported by your account</p>
              </div>
            </div>
            <span className="text-xs font-mono text-slate-400 bg-white/5 px-2.5 py-1 rounded-md">
              {profile?.products?.length || 0} Products
            </span>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 pt-2">
            {profile?.products && profile.products.length > 0 ? (
              profile.products.map((prod) => (
                <div
                  key={prod}
                  className={`p-3 rounded-xl border flex flex-col justify-between gap-1.5 transition-all hover:scale-[1.02] ${getProductColor(prod)}`}
                >
                  <span className="text-xs font-mono font-bold tracking-wider">{prod}</span>
                  <span className="text-[10px] text-slate-400 font-sans">
                    {prod === 'CNC' && 'Cash & Carry (Delivery)'}
                    {prod === 'MIS' && 'Margin Intraday Squareoff'}
                    {prod === 'NRML' && 'Normal (F&O Overnight)'}
                    {prod === 'BO' && 'Bracket Order'}
                    {prod === 'CO' && 'Cover Order'}
                    {!['CNC', 'MIS', 'NRML', 'BO', 'CO'].includes(prod) && 'Enabled product'}
                  </span>
                </div>
              ))
            ) : (
              <p className="text-xs text-slate-400 col-span-full">No product information available.</p>
            )}
          </div>
        </div>

        {/* Exchanges Section */}
        <div className="glass-panel p-6 space-y-4">
          <div className="flex items-center justify-between pb-3 border-b border-white/5">
            <div className="flex items-center gap-2">
              <div className="p-2 rounded-lg bg-emerald-500/10 text-emerald-400">
                <Globe className="w-5 h-5" />
              </div>
              <div>
                <h3 className="text-base font-bold text-white">Enabled Exchanges</h3>
                <p className="text-xs text-slate-400">Segments authorized for trading and order placement</p>
              </div>
            </div>
            <span className="text-xs font-mono text-slate-400 bg-white/5 px-2.5 py-1 rounded-md">
              {profile?.exchanges?.length || 0} Exchanges
            </span>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 pt-2">
            {profile?.exchanges && profile.exchanges.length > 0 ? (
              profile.exchanges.map((ex) => (
                <div
                  key={ex}
                  className={`p-3 rounded-xl border flex flex-col justify-between gap-1.5 transition-all hover:scale-[1.02] ${getExchangeColor(ex)}`}
                >
                  <span className="text-xs font-mono font-bold tracking-wider">{ex}</span>
                  <span className="text-[10px] text-slate-400 font-sans">
                    {ex === 'NSE' && 'National Stock Exchange'}
                    {ex === 'BSE' && 'Bombay Stock Exchange'}
                    {ex === 'NFO' && 'NSE Futures & Options'}
                    {ex === 'BFO' && 'BSE Futures & Options'}
                    {ex === 'MCX' && 'Multi Commodity Exch.'}
                    {ex === 'CDS' && 'Currency Derivatives'}
                    {!['NSE', 'BSE', 'NFO', 'BFO', 'MCX', 'CDS'].includes(ex) && 'Active segment'}
                  </span>
                </div>
              ))
            ) : (
              <p className="text-xs text-slate-400 col-span-full">No exchange information available.</p>
            )}
          </div>
        </div>
      </div>

      {/* Order Types & Account Details */}
      <div className="glass-panel p-6">
        <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-4 flex items-center gap-2">
          <Shield className="w-4 h-4 text-indigo-400" />
          Authorized Order Types
        </h3>
        <div className="flex flex-wrap gap-2.5">
          {profile?.order_types && profile.order_types.length > 0 ? (
            profile.order_types.map((type) => (
              <span
                key={type}
                className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-xs font-mono text-slate-200"
              >
                {type}
              </span>
            ))
          ) : (
            ['MARKET', 'LIMIT', 'SL', 'SL-M'].map((type) => (
              <span
                key={type}
                className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-xs font-mono text-slate-200"
              >
                {type}
              </span>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
