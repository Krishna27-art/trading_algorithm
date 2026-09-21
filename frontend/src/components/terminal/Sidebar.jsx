import React from 'react';
import {
  LayoutDashboard,
  Compass,
  Layers,
  FileText,
  ShieldCheck,
  BarChart3,
  Server,
  Settings,
  Zap,
  Radio,
  User,
  LogOut,
  TrendingUp,
} from 'lucide-react';

export default function Sidebar({
  activeTab,
  setActiveTab,
  user,
  onLogout,
  onOpenSettings,
  telemetry,
  tradingMode,
  setTradingMode,
}) {
  const currentStrat = (telemetry?.strategy || 'cpr').toUpperCase();
  const navItems = [
    { id: 'dashboard', label: 'Dashboard', icon: LayoutDashboard },
    { id: 'scanner', label: 'NIFTY 50 Scanner', icon: Compass, badge: 'LIVE' },
    { id: 'strategy', label: 'Strategy Engine', icon: Layers, badge: currentStrat },
    { id: 'positions', label: 'Positions', icon: Layers, count: telemetry?.active_trade ? 1 : 0 },
    { id: 'orders', label: 'Orders', icon: FileText },
    { id: 'risk', label: 'Risk Gate', icon: ShieldCheck, badge: '2%' },
    { id: 'performance', label: 'Performance', icon: BarChart3 },
    { id: 'system', label: 'System Health', icon: Server },
    { id: 'settings', label: 'Settings', icon: Settings },
  ];

  const isMarketOpen = telemetry?.market_status === 'OPEN';

  return (
    <aside className="w-64 bg-[#0d1322] border-r border-white/[0.08] flex flex-col justify-between select-none min-h-screen shrink-0 hidden md:flex">
      {/* Top Brand & Workspace Header */}
      <div className="p-4 border-b border-white/[0.08] space-y-3">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-gradient-to-br from-indigo-500 via-indigo-600 to-emerald-500 flex items-center justify-center font-black text-white text-base shadow-md shadow-indigo-500/20">
            K
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="font-extrabold text-slate-100 text-sm tracking-tight">Kite Algo Hub</h1>
              <span className="text-[10px] font-mono font-bold text-emerald-400 bg-emerald-500/10 px-1.5 py-0.5 rounded border border-emerald-500/20">
                PRO
              </span>
            </div>
            <p className="text-xs text-slate-400 font-mono">ORB + VWAP Intraday</p>
          </div>
        </div>

        {/* Paper vs Live Mode Toggle inside Sidebar */}
        <div className="p-1 bg-black/40 rounded-lg border border-white/10 flex items-center text-xs font-mono font-bold">
          <button
            onClick={() => setTradingMode('PAPER')}
            className={`flex-1 py-1.5 rounded-md transition-all text-center ${
              tradingMode === 'PAPER'
                ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            PAPER MODE
          </button>
          <button
            onClick={() => setTradingMode('LIVE')}
            className={`flex-1 py-1.5 rounded-md transition-all flex items-center justify-center gap-1.5 text-center ${
              tradingMode === 'LIVE'
                ? 'bg-amber-500/20 text-amber-300 border border-amber-500/40 shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            {tradingMode === 'LIVE' && <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />}
            LIVE REAL
          </button>
        </div>
      </div>

      {/* Navigation Links */}
      <nav className="p-3 space-y-1.5 flex-1">
        <div className="px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider text-slate-400">
          Terminal Workstation
        </div>

        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive = activeTab === item.id;

          return (
            <button
              key={item.id}
              onClick={() => {
                if (item.id === 'settings') {
                  onOpenSettings();
                } else {
                  setActiveTab(item.id);
                }
              }}
              className={`w-full flex items-center justify-between px-3.5 py-2.5 rounded-lg text-sm font-medium transition-all ${
                isActive
                  ? 'bg-indigo-600 text-white font-semibold shadow-md shadow-indigo-600/30'
                  : 'text-slate-300 hover:text-white hover:bg-white/[0.05]'
              }`}
            >
              <div className="flex items-center gap-3">
                <Icon className={`w-4 h-4 ${isActive ? 'text-white' : 'text-slate-400'}`} />
                <span>{item.label}</span>
              </div>

              {item.badge && (
                <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded font-bold ${
                  isActive ? 'bg-indigo-700 text-indigo-100' : 'bg-white/10 text-slate-300'
                }`}>
                  {item.badge}
                </span>
              )}

              {item.count !== undefined && item.count > 0 && (
                <span className="text-[10px] font-mono px-1.5 py-0.5 rounded-full font-bold bg-emerald-500 text-black">
                  {item.count}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      {/* Bottom User & Connection Info */}
      <div className="p-4 border-t border-white/[0.08] bg-black/20 space-y-3">
        {/* Connection status */}
        <div className="flex items-center justify-between text-xs font-mono">
          <div className="flex items-center gap-2">
            <span className={`status-dot ${isMarketOpen ? 'status-dot-emerald' : 'status-dot-rose'}`} />
            <span className="text-slate-300 font-semibold">{isMarketOpen ? 'NSE OPEN' : 'NSE CLOSED'}</span>
          </div>
          <span className="text-[11px] text-emerald-400 font-semibold">Kite Connected</span>
        </div>

        {/* User Card */}
        <div className="flex items-center justify-between p-2.5 rounded-lg bg-white/[0.03] border border-white/5">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-full bg-indigo-500/20 text-indigo-300 flex items-center justify-center font-bold text-xs border border-indigo-500/30">
              <User className="w-4 h-4" />
            </div>
            <div>
              <p className="text-xs font-bold text-slate-100 truncate max-w-[110px]">
                {user?.user_name || 'Bala Krishna'}
              </p>
              <p className="text-[11px] text-slate-400 font-mono">{user?.user_id || 'RHN918'}</p>
            </div>
          </div>

          <button
            onClick={onLogout}
            className="p-1.5 rounded-md text-slate-400 hover:text-rose-400 hover:bg-rose-500/10 transition-colors"
            title="Disconnect Zerodha Session"
          >
            <LogOut className="w-4 h-4" />
          </button>
        </div>
      </div>
    </aside>
  );
}
