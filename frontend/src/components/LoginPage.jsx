import React, { useState } from 'react';
import { Key, Lock, Compass, ExternalLink, ShieldCheck, HelpCircle, ArrowRight, Loader2, AlertCircle } from 'lucide-react';

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

  return (
    <div className="min-h-screen flex items-center justify-center p-4 relative">
      {/* Background Decorative Blur */}
      <div className="absolute top-1/4 left-1/2 -translate-x-1/2 -translate-y-1/2 w-96 h-96 bg-indigo-600/10 rounded-full blur-3xl pointer-events-none" />

      <div className="w-full max-w-lg glass-panel p-8 relative z-10 animate-fade-in border border-white/10 shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between pb-6 border-b border-white/10 mb-6">
          <div className="flex items-center gap-3">
            <div className="w-11 h-11 rounded-xl bg-gradient-to-br from-indigo-500 to-emerald-500 flex items-center justify-center shadow-lg shadow-indigo-500/20">
              <Compass className="w-6 h-6 text-white" />
            </div>
            <div>
              <h1 className="text-xl font-bold text-white tracking-tight">Kite Connect Algo</h1>
              <p className="text-xs text-slate-400">Zerodha Semi-Automated Trading Hub</p>
            </div>
          </div>
          <span className="badge badge-emerald">
            <ShieldCheck className="w-3.5 h-3.5" /> Secure Backend
          </span>
        </div>

        {/* Error Notification */}
        {error && (
          <div className="mb-6 p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm flex items-start gap-3 animate-fade-in">
            <AlertCircle className="w-5 h-5 flex-shrink-0 text-rose-400 mt-0.5" />
            <div className="leading-snug">{error}</div>
          </div>
        )}

        {/* Form */}
        <form onSubmit={handleSubmit} className="space-y-5">
          {/* API Key */}
          <div>
            <label className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2 flex items-center gap-1.5">
              <Key className="w-3.5 h-3.5 text-indigo-400" />
              API Key
            </label>
            <input
              type="text"
              className="custom-input"
              placeholder="e.g. abcdef123456"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              autoComplete="off"
              required
            />
          </div>

          {/* API Secret */}
          <div>
            <label className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2 flex items-center gap-1.5">
              <Lock className="w-3.5 h-3.5 text-indigo-400" />
              API Secret
            </label>
            <input
              type="password"
              className="custom-input"
              placeholder="••••••••••••••••••••"
              value={apiSecret}
              onChange={(e) => setApiSecret(e.target.value)}
              autoComplete="off"
              required
            />
          </div>

          {/* Request Token with helper */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-xs font-semibold text-slate-300 uppercase tracking-wider flex items-center gap-1.5">
                <Compass className="w-3.5 h-3.5 text-emerald-400" />
                Request Token
              </label>
              <button
                type="button"
                onClick={handleOpenKiteLogin}
                className="text-xs text-indigo-400 hover:text-indigo-300 flex items-center gap-1 font-medium transition-colors cursor-pointer"
              >
                <span>Get Request Token</span>
                <ExternalLink className="w-3 h-3" />
              </button>
            </div>
            <input
              type="text"
              className="custom-input"
              placeholder="Paste request_token or full redirected URL"
              value={requestToken}
              onChange={(e) => setRequestToken(e.target.value)}
              autoComplete="off"
              required
            />
            <p className="text-[11px] text-slate-400 mt-1.5">
              Tip: Click "Get Request Token", log into Zerodha, and paste the URL or token here.
            </p>
          </div>

          {/* Submit Button */}
          <button
            type="submit"
            disabled={loading}
            className="btn-primary w-full py-3 mt-4 text-base"
          >
            {loading ? (
              <>
                <Loader2 className="w-5 h-5 animate-spin" />
                <span>Authenticating Session...</span>
              </>
            ) : (
              <>
                <span>Login & Open Dashboard</span>
                <ArrowRight className="w-4 h-4" />
              </>
            )}
          </button>
        </form>

        {/* Security / Help Info */}
        <div className="mt-6 pt-5 border-t border-white/5 flex flex-col gap-3 text-xs text-slate-400">
          <div className="flex items-center gap-2 text-emerald-400">
            <ShieldCheck className="w-4 h-4 flex-shrink-0" />
            <span>Access token is safely saved on your backend and never exposed in browser.</span>
          </div>

          <button
            type="button"
            onClick={() => setShowGuide(!showGuide)}
            className="flex items-center gap-1.5 text-slate-400 hover:text-slate-200 transition-colors cursor-pointer w-fit"
          >
            <HelpCircle className="w-3.5 h-3.5" />
            <span>{showGuide ? 'Hide setup instructions' : 'How does Zerodha Kite login work?'}</span>
          </button>

          {showGuide && (
            <div className="p-4 rounded-xl bg-slate-900/90 border border-white/10 text-slate-300 text-xs space-y-2 animate-fade-in mt-1">
              <p className="font-semibold text-indigo-300">Daily Login Flow:</p>
              <ol className="list-decimal pl-4 space-y-1.5 text-slate-300">
                <li>Create an app on <a href="https://kite.trade" target="_blank" rel="noreferrer" className="text-indigo-400 underline">kite.trade</a> and copy your API Key & Secret.</li>
                <li>Set your Redirect URL in your Kite app to <code className="text-emerald-300 bg-white/5 px-1 py-0.5 rounded">http://127.0.0.1:8000/</code> or <code className="text-emerald-300 bg-white/5 px-1 py-0.5 rounded">http://localhost/</code>.</li>
                <li>Enter your API Key & click <strong>"Get Request Token"</strong>. Log in with your password & TOTP.</li>
                <li>Copy the <code className="text-emerald-300 bg-white/5 px-1 py-0.5 rounded">request_token</code> from the browser address bar and hit <strong>Login</strong>.</li>
              </ol>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
