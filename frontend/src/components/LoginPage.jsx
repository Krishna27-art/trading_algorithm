import React, { useState } from 'react';
import { Key, Lock, Compass, ExternalLink, ShieldCheck, HelpCircle, ArrowRight, Loader2, AlertCircle, Play } from 'lucide-react';

export default function LoginPage({ onLoginSuccess }) {
  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');
  const [requestToken, setRequestToken] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [showGuide, setShowGuide] = useState(false);

  const handleOpenKiteLogin = async () => {
    if (!apiKey.trim()) {
      setError('Please enter your API Key first to generate the login link.');
      return;
    }
    setError('');
    try {
      const res = await fetch('/api/login-url', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: apiKey.trim() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Could not generate login link');
      
      // Open in new tab
      window.open(data.login_url, '_blank', 'noopener,noreferrer');
    } catch (err) {
      setError(err.message);
    }
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!apiKey.trim() || !apiSecret.trim() || !requestToken.trim()) {
      setError('Please provide API Key, API Secret, and Request Token.');
      return;
    }

    setLoading(true);
    setError('');

    try {
      const res = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          api_key: apiKey.trim(),
          api_secret: apiSecret.trim(),
          request_token: requestToken.trim(),
        }),
      });

      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || 'Authentication failed. Please check your tokens.');
      }

      onLoginSuccess(data.user);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleEnterPaperTerminal = () => {
    onLoginSuccess({
      user_id: 'PAPER',
      user_name: 'Paper Trader',
      login_time: new Date().toISOString(),
      api_key: 'PAPER',
    });
  };

  return (
    <div className="min-h-screen flex items-center justify-center p-4 bg-[#070a11] text-slate-100 select-none">
      <div className="w-full max-w-md term-panel p-6 space-y-4 border-white/10 shadow-2xl animate-fade-in">
        {/* Header */}
        <div className="flex items-center justify-between pb-4 border-b border-white/[0.08]">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded bg-gradient-to-br from-indigo-500 to-emerald-500 flex items-center justify-center font-bold text-white text-sm">
              K
            </div>
            <div>
              <h1 className="text-sm font-bold text-white tracking-tight">Kite Algo Hub</h1>
              <p className="text-[11px] text-slate-400 font-mono">Zerodha ORB + VWAP Terminal</p>
            </div>
          </div>
          <span className="term-badge term-badge-emerald text-[10px] font-mono">
            <ShieldCheck className="w-3 h-3" /> SECURE
          </span>
        </div>

        {/* Error Notification */}
        {error && (
          <div className="p-2.5 rounded bg-rose-500/10 border border-rose-500/30 text-rose-300 text-xs flex items-start gap-2 animate-fade-in">
            <AlertCircle className="w-4 h-4 flex-shrink-0 text-rose-400 mt-0.5" />
            <div className="leading-snug">{error}</div>
          </div>
        )}

        {/* Form */}
        <form onSubmit={handleSubmit} className="space-y-3 font-mono text-xs">
          {/* API Key */}
          <div>
            <label className="text-[10px] font-semibold text-slate-300 uppercase tracking-wider block mb-1">
              Kite API Key
            </label>
            <input
              type="text"
              className="term-input"
              placeholder="e.g. sswg9s60swc2dizh"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              autoComplete="off"
              required
            />
          </div>

          {/* API Secret */}
          <div>
            <label className="text-[10px] font-semibold text-slate-300 uppercase tracking-wider block mb-1">
              Kite API Secret
            </label>
            <input
              type="password"
              className="term-input"
              placeholder="••••••••••••••••••••"
              value={apiSecret}
              onChange={(e) => setApiSecret(e.target.value)}
              autoComplete="off"
              required
            />
          </div>

          {/* Request Token with helper */}
          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-[10px] font-semibold text-slate-300 uppercase tracking-wider">
                Request Token
              </label>
              <button
                type="button"
                onClick={handleOpenKiteLogin}
                className="text-[10px] text-indigo-400 hover:text-indigo-300 flex items-center gap-1 font-sans font-medium transition-colors cursor-pointer"
              >
                <span>Get Request Token</span>
                <ExternalLink className="w-2.5 h-2.5" />
              </button>
            </div>
            <input
              type="text"
              className="term-input"
              placeholder="Paste request_token or redirected URL"
              value={requestToken}
              onChange={(e) => setRequestToken(e.target.value)}
              autoComplete="off"
            />
            <p className="text-[10px] text-slate-400 font-sans mt-1">
              Click "Get Request Token", authenticate in Zerodha, then paste the token here.
            </p>
          </div>

          {/* Submit Button */}
          <button
            type="submit"
            disabled={loading}
            className="w-full py-2.5 mt-2 term-btn-primary font-bold text-xs"
          >
            {loading ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                <span>Exchanging Token with Zerodha...</span>
              </>
            ) : (
              <>
                <span>Authenticate Live Kite Session</span>
                <ArrowRight className="w-3.5 h-3.5" />
              </>
            )}
          </button>
        </form>

        {/* Alternative: Enter Paper Trading Terminal Immediately */}
        <div className="pt-2 border-t border-white/[0.06] space-y-2">
          <button
            type="button"
            onClick={handleEnterPaperTerminal}
            className="w-full py-2 term-btn-secondary text-xs font-mono font-semibold flex items-center justify-center gap-2"
          >
            <Play className="w-3.5 h-3.5 text-emerald-400" />
            <span>Open Workstation in Paper Trading Mode</span>
          </button>

          <p className="text-[10px] text-center text-slate-400 font-sans">
            Paper mode allows full simulation, backtest analysis, and charts without live broker login.
          </p>
        </div>
      </div>
    </div>
  );
}
