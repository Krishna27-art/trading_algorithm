import React from 'react';
import { Activity, Radio, Zap, Cpu, Shield, Server } from 'lucide-react';

export default function SystemHealthBar({ health }) {
  const h = health || {
    kite_api: 'CONNECTED',
    market_data: 'CONNECTED',
    strategy_engine: 'RUNNING',
    risk_engine: 'RUNNING',
    order_manager: 'READY',
    latency_ms: 38,
  };

  const getStatusColor = (val) => {
    if (val === 'CONNECTED' || val === 'STREAMING' || val === 'RUNNING' || val === 'READY' || val === 'ARMED') {
      return 'text-emerald-400 font-bold';
    }
    if (val?.includes('AUTH') || val?.includes('SIMULATED') || val?.includes('WAITING')) {
      return 'text-amber-400 font-bold';
    }
    return 'text-rose-400 font-bold';
  };

  return (
    <div className="term-panel p-3 px-4 flex flex-wrap items-center justify-between gap-4 text-xs font-mono select-none">
      <div className="flex items-center gap-2">
        <Activity className="w-4 h-4 text-indigo-400" />
        <span className="font-extrabold text-white uppercase tracking-wider text-xs font-sans">
          Subsystem Health
        </span>
      </div>

      <div className="flex flex-wrap items-center gap-4 sm:gap-6">
        <div className="flex items-center gap-1.5">
          <Radio className="w-3.5 h-3.5 text-slate-400" />
          <span className="text-slate-400">KITE API:</span>
          <span className={getStatusColor(h.kite_api)}>{h.kite_api}</span>
        </div>

        <div className="flex items-center gap-1.5">
          <Zap className="w-3.5 h-3.5 text-slate-400" />
          <span className="text-slate-400">MARKET DATA:</span>
          <span className={getStatusColor(h.market_data)}>{h.market_data}</span>
        </div>

        <div className="flex items-center gap-1.5">
          <Cpu className="w-3.5 h-3.5 text-slate-400" />
          <span className="text-slate-400">STRATEGY ENGINE:</span>
          <span className={getStatusColor(h.strategy_engine)}>{h.strategy_engine}</span>
        </div>

        <div className="flex items-center gap-1.5">
          <Shield className="w-3.5 h-3.5 text-slate-400" />
          <span className="text-slate-400">RISK ENGINE:</span>
          <span className={getStatusColor(h.risk_engine)}>{h.risk_engine}</span>
        </div>

        <div className="flex items-center gap-1.5">
          <Server className="w-3.5 h-3.5 text-slate-400" />
          <span className="text-slate-400">ORDER MANAGER:</span>
          <span className={getStatusColor(h.order_manager || 'READY')}>{h.order_manager || 'READY'}</span>
        </div>

        <div className="flex items-center gap-1.5 text-slate-300">
          <span className="text-slate-400">LAST UPDATE:</span>
          <span className="text-cyan-300 font-bold">{h.latency_ms || 38} ms ago</span>
        </div>
      </div>
    </div>
  );
}
