import React, { useState, useEffect } from 'react';
import { Settings, LogOut, Radio, User, Activity, Menu } from 'lucide-react';

export default function TopNavbar({
  user,
  telemetry,
  onLogout,
  onOpenSettings,
  tradingMode,
  setTradingMode,
  onToggleMobileSidebar,
}) {
  const [istTime, setIstTime] = useState('');

  useEffect(() => {
    const updateTime = () => {
      const now = new Date();
      const options = {
        timeZone: 'Asia/Kolkata',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      };
      setIstTime(new Intl.DateTimeFormat('en-GB', options).format(now));
    };
    updateTime();
    const timer = setInterval(updateTime, 1000);
    return () => clearInterval(timer);
  }, []);

  const isMarketOpen = telemetry?.market_status === 'OPEN';
  const ltp = telemetry?.current_price || 24150.0;
  const changePts = telemetry?.price_change_pts || 0.0;
  const changePct = telemetry?.price_change_pct || 0.0;
  const algoState = telemetry?.algorithm_state || 'RUNNING';

  return (
    <header className="h-14 bg-[#0d1322] border-b border-white/[0.08] px-4 lg:px-6 flex items-center justify-between select-none sticky top-0 z-40">
      {/* LEFT: Brand & Algorithm Indicator */}
      <div className="flex items-center gap-3">
        {onToggleMobileSidebar && (
          <button
            onClick={onToggleMobileSidebar}
            className="p-1.5 rounded text-slate-400 hover:text-white md:hidden"
          >
            <Menu className="w-5 h-5" />
          </button>
        )}

        <div className="flex items-center gap-2.5">
          <div className="w-7 h-7 rounded-md bg-indigo-600 flex items-center justify-center font-black text-white text-xs tracking-wider shadow-sm">
            K
          </div>
          <div className="flex items-baseline gap-2">
            <span className="font-extrabold text-white text-sm sm:text-base tracking-tight">
              Kite Algo Hub
            </span>
            <span className="hidden sm:inline-block text-[11px] font-mono font-semibold text-indigo-300 bg-indigo-500/10 px-2 py-0.5 rounded border border-indigo-500/20">
              ORB + VWAP
            </span>
          </div>
        </div>
      </div>

      {/* CENTER: NIFTY 50 LIVE PRICE & CHANGE */}
      <div className="flex items-center gap-3 sm:gap-4 bg-black/40 px-3 sm:px-4 py-1.5 rounded-lg border border-white/[0.08]">
        <div className="flex items-center gap-1.5">
          <span className="text-xs sm:text-sm font-extrabold text-white tracking-tight">NIFTY 50</span>
          <span className="text-[10px] font-mono text-slate-400 hidden sm:inline">NFO FUT</span>
        </div>

        <div className="h-4 w-[1px] bg-white/10" />

        <div className="flex items-baseline gap-2 font-mono">
          <span className="text-sm sm:text-base font-extrabold text-white tracking-tight">
            ₹{ltp.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </span>
          <span
            className={`text-xs font-bold ${
              changePts >= 0 ? 'text-emerald-400' : 'text-rose-400'
            }`}
          >
            {changePts >= 0 ? '+' : ''}
            {changePts.toFixed(2)} ({changePct >= 0 ? '+' : ''}
            {changePct.toFixed(2)}%)
          </span>
        </div>
      </div>

      {/* RIGHT: Status Pills & Actions */}
      <div className="flex items-center gap-2 sm:gap-3">
        {/* Kite Connection Status */}
        <div className="hidden lg:flex items-center gap-1.5 text-xs font-mono px-2.5 py-1 rounded bg-white/[0.03] border border-white/5">
          <span className="w-2 h-2 rounded-full bg-emerald-400" />
          <span className="text-slate-300">KITE CONNECTED</span>
        </div>

        {/* Market Status & Clock */}
        <div className="flex items-center gap-1.5 text-xs font-mono px-2.5 py-1 rounded bg-white/[0.03] border border-white/5">
          <span className={`status-dot ${isMarketOpen ? 'status-dot-emerald' : 'status-dot-rose'}`} />
          <span className="font-bold text-slate-200">
            {isMarketOpen ? 'MARKET OPEN' : 'MARKET CLOSED'}
          </span>
          <span className="text-slate-400 hidden sm:inline">({istTime} IST)</span>
        </div>

        {/* Algo Status Pill */}
        <div className="hidden sm:flex items-center gap-1.5 text-xs font-mono px-2.5 py-1 rounded bg-indigo-500/10 border border-indigo-500/20">
          <Activity className="w-3.5 h-3.5 text-indigo-400" />
          <span className="text-indigo-300 font-bold">ALGO: {algoState}</span>
        </div>

        {/* Settings button */}
        <button
          onClick={onOpenSettings}
          className="p-2 rounded-lg text-slate-300 hover:text-white hover:bg-white/10 border border-white/5 transition-colors"
          title="Terminal Settings"
        >
          <Settings className="w-4 h-4" />
        </button>

        {/* Logout button */}
        <button
          onClick={onLogout}
          className="p-2 rounded-lg text-rose-400 hover:text-rose-300 hover:bg-rose-500/10 border border-rose-500/20 transition-colors hidden sm:block"
          title="Logout"
        >
          <LogOut className="w-4 h-4" />
        </button>
      </div>
    </header>
  );
}
