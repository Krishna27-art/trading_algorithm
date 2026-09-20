import React, { useState } from 'react';
import { User, Wallet, Cpu, BookOpen, LogOut, ShieldCheck, Compass, Radio } from 'lucide-react';
import UserTab from './tabs/UserTab';
import MarginsTab from './tabs/MarginsTab';
import StrategyTab from './tabs/StrategyTab';
import GuideTab from './tabs/GuideTab';

export default function Dashboard({ user, onLogout }) {
  const [activeTab, setActiveTab] = useState('user');
  const [loggingOut, setLoggingOut] = useState(false);

  const handleLogout = async () => {
    setLoggingOut(true);
    try {
      await fetch('/api/logout', { method: 'POST' });
    } catch (e) {
      console.error('Logout error:', e);
    } finally {
      onLogout();
      setLoggingOut(false);
    }
  };

  const tabs = [
    { id: 'user', label: 'User Profile', icon: User },
    { id: 'margins', label: 'Margins & Funds', icon: Wallet },
    { id: 'strategy', label: 'Semi-Auto Algo', icon: Cpu },
    { id: 'guide', label: 'Setup & Instructions', icon: BookOpen },
  ];

  return (
    <div className="min-h-screen flex flex-col">
      {/* Top Navbar */}
      <header className="glass-panel rounded-none border-x-0 border-t-0 border-b border-white/10 sticky top-0 z-30 px-4 sm:px-8 py-3.5 backdrop-blur-xl bg-slate-950/80">
        <div className="max-w-7xl mx-auto flex items-center justify-between">
          {/* Logo & Brand */}
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-indigo-500 to-emerald-500 flex items-center justify-center shadow-lg shadow-indigo-500/20">
              <Compass className="w-5 h-5 text-white" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="font-extrabold text-white tracking-tight text-base sm:text-lg">Kite Algo Hub</span>
                <span className="badge badge-emerald hidden sm:inline-flex">
                  <Radio className="w-3 h-3 animate-pulse text-emerald-400" />
                  Live Session
                </span>
              </div>
              <p className="text-[11px] text-slate-400">Zerodha Semi-Automated Trading</p>
            </div>
          </div>

          {/* User Status & Logout */}
          <div className="flex items-center gap-3 sm:gap-4">
            <div className="hidden sm:flex flex-col text-right">
              <span className="text-xs font-bold text-slate-200">{user?.user_name || 'Trader'}</span>
              <span className="text-[10px] font-mono text-slate-400">ID: {user?.user_id}</span>
            </div>

            <button
              onClick={handleLogout}
              disabled={loggingOut}
              className="btn-danger-subtle"
              title="Log out and clear session"
            >
              <LogOut className="w-4 h-4" />
              <span className="hidden sm:inline">Logout</span>
            </button>
          </div>
        </div>
      </header>

      {/* Main Content Area */}
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-8 py-6 space-y-6">
        {/* Navigation Tabs */}
        <div className="glass-panel p-1.5 flex items-center gap-1 overflow-x-auto border border-white/10">
          {tabs.map((t) => {
            const Icon = t.icon;
            const isActive = activeTab === t.id;
            return (
              <button
                key={t.id}
                onClick={() => setActiveTab(t.id)}
                className={`tab-btn rounded-xl flex-1 justify-center whitespace-nowrap ${
                  isActive ? 'active shadow-sm' : ''
                }`}
              >
                <Icon className={`w-4 h-4 ${isActive ? 'text-indigo-400' : 'text-slate-400'}`} />
                <span>{t.label}</span>
              </button>
            );
          })}
        </div>

        {/* Tab Content Panels */}
        <div className="py-2">
          {activeTab === 'user' && <UserTab />}
          {activeTab === 'margins' && <MarginsTab />}
          {activeTab === 'strategy' && <StrategyTab />}
          {activeTab === 'guide' && <GuideTab />}
        </div>
      </main>

      {/* Footer */}
      <footer className="border-t border-white/5 py-4 px-4 text-center text-xs text-slate-500">
        <p>Zerodha Kite Connect Semi-Automated Algorithm • Session preserved for local development</p>
      </footer>
    </div>
  );
}
