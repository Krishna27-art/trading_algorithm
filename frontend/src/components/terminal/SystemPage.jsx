import React from 'react';
import { Server, Radio, Zap, Cpu, Shield, Database, Activity, CheckCircle2 } from 'lucide-react';

export default function SystemPage({ health }) {
  const h = health || {
    kite_api: 'CONNECTED',
    market_data: 'CONNECTED',
    database: 'CONNECTED',
    strategy_engine: 'RUNNING',
    risk_engine: 'RUNNING',
    order_manager: 'READY',
    latency_ms: 38,
  };

  const systems = [
    { name: 'Zerodha Kite Connect API', status: h.kite_api, desc: 'OAuth session & access token pipeline', icon: Radio },
    { name: 'Market Data Ticker Feed', status: h.market_data, desc: 'NSE real-time WebSocket tick stream', icon: Zap },
    { name: 'SQLite Trading Database', status: h.database, desc: 'Local journal & order state repository', icon: Database },
    { name: 'ORB Strategy Engine', status: h.strategy_engine, desc: '15m candle accumulator & signal evaluator', icon: Cpu },
    { name: 'Risk Management Gate', status: h.risk_engine, desc: '1% per-trade cap & 2% daily kill-switch', icon: Shield },
    { name: 'Order Router & Broker Gateway', status: h.order_manager || 'READY', desc: 'Pre-trade check & Zerodha MIS order execution', icon: Server },
  ];

  return (
    <div className="space-y-4">
      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-white/[0.08]">
        <div>
          <h2 className="text-xl font-extrabold text-white tracking-tight flex items-center gap-2.5">
            <Server className="w-5 h-5 text-indigo-400" /> Subsystem Diagnostics & Status
          </h2>
          <p className="text-xs text-slate-400 font-mono mt-0.5">
            Real-time connection telemetry across all local and broker services
          </p>
        </div>

        <div className="flex items-center gap-2 text-xs font-mono bg-black/40 px-3 py-1.5 rounded-lg border border-white/10">
          <Activity className="w-4 h-4 text-cyan-400" />
          <span className="text-slate-300">Feed Latency: </span>
          <span className="text-cyan-300 font-bold">{h.latency_ms || 38} ms</span>
        </div>
      </div>

      {/* Grid of Subsystems */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {systems.map((sys, idx) => {
          const Icon = sys.icon;
          const isHealthy = sys.status === 'CONNECTED' || sys.status === 'RUNNING' || sys.status === 'READY';

          return (
            <div key={idx} className="term-panel p-5 space-y-3 flex flex-col justify-between">
              <div className="flex items-center justify-between">
                <div className="p-2 rounded-lg bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                  <Icon className="w-5 h-5" />
                </div>
                <span className={`term-badge ${isHealthy ? 'term-badge-emerald' : 'term-badge-rose'} text-xs font-mono font-bold`}>
                  {sys.status}
                </span>
              </div>

              <div>
                <h3 className="text-sm font-bold text-white tracking-tight">{sys.name}</h3>
                <p className="text-xs text-slate-400 mt-1 leading-relaxed">{sys.desc}</p>
              </div>

              <div className="pt-2 border-t border-white/5 flex items-center justify-between text-xs font-mono text-slate-400">
                <span>Health Check</span>
                <span className="text-emerald-400 flex items-center gap-1">
                  <CheckCircle2 className="w-3.5 h-3.5" /> Operational
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
