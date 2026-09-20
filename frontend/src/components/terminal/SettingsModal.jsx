import React, { useState } from 'react';
import { X, Sliders, Shield, Key, Cpu, Bell, Server, Info } from 'lucide-react';

export default function SettingsModal({ onClose, strategyState, user }) {
  const [activeTab, setActiveTab] = useState('strategy');

  const tabs = [
    { id: 'strategy', label: 'Strategy', icon: Cpu },
    { id: 'risk', label: 'Risk Rules', icon: Shield },
    { id: 'broker', label: 'Broker Config', icon: Key },
    { id: 'trading', label: 'Trading Schedule', icon: Sliders },
    { id: 'system', label: 'System Info', icon: Server },
  ];

  return (
    <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="w-full max-w-2xl term-panel p-5 space-y-4 border-white/10 shadow-2xl animate-fade-in">
        {/* Header */}
        <div className="flex items-center justify-between pb-3 border-b border-white/10">
          <div className="flex items-center gap-2 text-indigo-400">
            <Sliders className="w-5 h-5" />
            <h2 className="text-sm font-bold text-white uppercase tracking-wider">Trading System Parameters (Read-Only)</h2>
          </div>
          <button onClick={onClose} className="p-1 rounded text-slate-400 hover:text-white">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Tab Pills */}
        <div className="flex items-center gap-1 overflow-x-auto pb-1 border-b border-white/5 text-xs font-mono">
          {tabs.map((t) => {
            const Icon = t.icon;
            const isActive = activeTab === t.id;
            return (
              <button
                key={t.id}
                onClick={() => setActiveTab(t.id)}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded transition-colors ${
                  isActive ? 'bg-indigo-600 text-white font-semibold' : 'text-slate-400 hover:text-white'
                }`}
              >
                <Icon className="w-3.5 h-3.5" />
                <span>{t.label}</span>
              </button>
            );
          })}
        </div>

        {/* Tab Content */}
        <div className="py-2 text-xs font-mono space-y-3">
          {activeTab === 'strategy' && (
            <div className="space-y-2">
              <div className="p-2.5 rounded bg-white/[0.02] border border-white/5 space-y-1">
                <span className="text-slate-400 text-[10px] uppercase block">Strategy Model</span>
                <span className="text-slate-100 font-bold text-sm">30-Minute Volatility-Filtered ORB + VWAP</span>
                <p className="text-slate-400 text-[11px] font-sans">
                  Evaluates 15-minute candle closes against the 09:15–09:45 opening range high and low with session volume-weighted average price confirmation.
                </p>
              </div>

              <div className="grid grid-cols-2 gap-2">
                <div className="p-2 rounded bg-black/30 border border-white/5">
                  <span className="text-slate-400 text-[10px] block">Min ORB Width Filter:</span>
                  <span className="text-emerald-400 font-bold">40.0 Index Points</span>
                </div>
                <div className="p-2 rounded bg-black/30 border border-white/5">
                  <span className="text-slate-400 text-[10px] block">Max Tail-Risk Cap:</span>
                  <span className="text-indigo-300 font-bold">80.0 Index Points (if Width &gt; 120)</span>
                </div>
                <div className="p-2 rounded bg-black/30 border border-white/5">
                  <span className="text-slate-400 text-[10px] block">Reward-to-Risk (R:R):</span>
                  <span className="text-slate-200 font-bold">2.0 : 1.0 Payoff</span>
                </div>
                <div className="p-2 rounded bg-black/30 border border-white/5">
                  <span className="text-slate-400 text-[10px] block">Breakeven Trailing Trigger:</span>
                  <span className="text-emerald-400 font-bold">+1.0R Unrealized Gain</span>
                </div>
              </div>
            </div>
          )}

          {activeTab === 'risk' && (
            <div className="space-y-2">
              <div className="grid grid-cols-2 gap-2">
                <div className="p-2.5 rounded bg-black/30 border border-white/5">
                  <span className="text-slate-400 text-[10px] block">Risk Budget per Trade:</span>
                  <span className="text-white font-bold text-sm">1.0% Capital (₹10,000)</span>
                </div>
                <div className="p-2.5 rounded bg-black/30 border border-white/5">
                  <span className="text-slate-400 text-[10px] block">Daily Portfolio Circuit-Breaker:</span>
                  <span className="text-rose-400 font-bold text-sm">2.0% Capital (₹20,000 Loss)</span>
                </div>
                <div className="p-2.5 rounded bg-black/30 border border-white/5">
                  <span className="text-slate-400 text-[10px] block">Daily Trade Limit:</span>
                  <span className="text-slate-200 font-bold">Max 1 Trade / Day</span>
                </div>
                <div className="p-2.5 rounded bg-black/30 border border-white/5">
                  <span className="text-slate-400 text-[10px] block">Position Sizing Formula:</span>
                  <span className="text-indigo-300 font-bold">floor(Risk / R_trade) rounded to lot</span>
                </div>
              </div>
            </div>
          )}

          {activeTab === 'broker' && (
            <div className="space-y-2">
              <div className="p-2.5 rounded bg-white/[0.02] border border-white/5 space-y-1.5">
                <div className="flex justify-between">
                  <span className="text-slate-400">Broker:</span>
                  <span className="text-slate-200 font-bold">Zerodha Kite Connect v3</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-400">User ID:</span>
                  <span className="text-slate-200 font-bold">{user?.user_id || 'RHN918'}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-400">API Key:</span>
                  <span className="text-indigo-300 font-mono">sswg9s60swc2dizh</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-400">Auth Token Storage:</span>
                  <span className="text-emerald-400 font-semibold">Backend Local File (session_token.json)</span>
                </div>
              </div>
            </div>
          )}

          {activeTab === 'trading' && (
            <div className="space-y-2">
              <div className="grid grid-cols-2 gap-2">
                <div className="p-2 rounded bg-black/30 border border-white/5">
                  <span className="text-slate-400 text-[10px] block">Market Preparation:</span>
                  <span className="text-slate-200">Before 09:15 IST</span>
                </div>
                <div className="p-2 rounded bg-black/30 border border-white/5">
                  <span className="text-slate-400 text-[10px] block">Opening Range Window:</span>
                  <span className="text-slate-200">09:15 – 09:45 IST</span>
                </div>
                <div className="p-2 rounded bg-black/30 border border-white/5">
                  <span className="text-slate-400 text-[10px] block">Entry Scanning Cutoff:</span>
                  <span className="text-slate-200">13:30 IST</span>
                </div>
                <div className="p-2 rounded bg-black/30 border border-white/5">
                  <span className="text-slate-400 text-[10px] block">Mandatory Square-Off:</span>
                  <span className="text-rose-400 font-bold">14:30 IST</span>
                </div>
              </div>
            </div>
          )}

          {activeTab === 'system' && (
            <div className="space-y-2">
              <div className="p-2 rounded bg-black/30 border border-white/5 space-y-1">
                <div className="flex justify-between">
                  <span className="text-slate-400">Python Runtime:</span>
                  <span className="text-slate-200">Python 3.14.7 (.venv)</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-400">FastAPI Backend:</span>
                  <span className="text-slate-200">http://127.0.0.1:8000</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-400">React + Vite Frontend:</span>
                  <span className="text-slate-200">http://localhost:5173</span>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="pt-2 border-t border-white/10 flex justify-end">
          <button onClick={onClose} className="term-btn-secondary py-1.5 px-4 text-xs font-mono">
            Close Settings
          </button>
        </div>
      </div>
    </div>
  );
}
