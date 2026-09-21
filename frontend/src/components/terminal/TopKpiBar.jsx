import React from 'react';
import { TrendingUp, TrendingDown, Shield, Activity, Percent, Layers, Wallet } from 'lucide-react';

export default function TopKpiBar({ telemetry, margins }) {
  const risk = telemetry?.risk_summary || {};
  const capital = risk.capital;
  const availableMargin = margins?.equity?.available?.live_balance;
  const pnl = telemetry?.active_trade?.unrealized_pnl || 0.0;
  const pnlPct = capital ? (pnl / capital) * 100.0 : 0.0;
  const tradesTaken = risk.trades_taken || 0;
  const maxTrades = risk.max_trades || 1;
  const riskUsed = risk.daily_risk_used || 0;
  const riskLimit = risk.daily_risk_limit || (capital ? capital * 0.02 : null);
  const algoState = telemetry?.algorithm_state || 'RUNNING';

  const formatINR = (val) => {
    if (val === null || val === undefined) return '—';
    return new Intl.NumberFormat('en-IN', {
      style: 'currency',
      currency: 'INR',
      maximumFractionDigits: 0,
    }).format(val);
  };

  const getStatusColor = (state) => {
    if (state.includes('SIGNAL') || state.includes('ACTIVE')) return 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30';
    if (state.includes('WAITING') || state.includes('BUILDING') || state.includes('RUNNING')) return 'text-indigo-400 bg-indigo-500/10 border-indigo-500/30';
    if (state.includes('LIMIT') || state.includes('HIT') || state.includes('STOP')) return 'text-amber-400 bg-amber-500/10 border-amber-500/30';
    return 'text-slate-400 bg-white/5 border-white/10';
  };

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-7 gap-3">
      {/* 1. CAPITAL */}
      <div className="term-panel p-3.5 flex flex-col justify-between">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Capital</span>
          <Wallet className="w-3.5 h-3.5 text-indigo-400" />
        </div>
        <div className="mt-2">
          <span className="text-xl sm:text-2xl font-bold font-mono text-white tracking-tight">
            {formatINR(capital)}
          </span>
          <span className="text-xs text-slate-400 block font-mono mt-0.5">Base Portfolio</span>
        </div>
      </div>

      {/* 2. AVAILABLE MARGIN */}
      <div className="term-panel p-3.5 flex flex-col justify-between">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Available Margin</span>
          <span className="text-[11px] font-mono text-emerald-400 font-bold bg-emerald-500/10 px-1.5 py-0.5 rounded">MIS</span>
        </div>
        <div className="mt-2">
          <span className="text-xl sm:text-2xl font-bold font-mono text-emerald-300 tracking-tight">
            {formatINR(availableMargin)}
          </span>
          <span className="text-xs text-slate-400 block font-mono mt-0.5">Intraday Power</span>
        </div>
      </div>

      {/* 3. TODAY P&L */}
      <div className={`term-panel p-3.5 flex flex-col justify-between ${
        pnl > 0 ? 'border-emerald-500/30 bg-emerald-500/[0.03]' : pnl < 0 ? 'border-rose-500/30 bg-rose-500/[0.03]' : ''
      }`}>
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Today's P&L</span>
          <span className={`text-xs font-mono font-bold ${pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
            {pnl >= 0 ? '+' : ''}{pnlPct.toFixed(2)}%
          </span>
        </div>
        <div className="mt-2">
          <span className={`text-xl sm:text-2xl font-bold font-mono tracking-tight ${
            pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'
          }`}>
            {pnl >= 0 ? '+' : ''}{formatINR(pnl)}
          </span>
          <span className="text-xs text-slate-400 block font-mono mt-0.5">
            {telemetry?.active_trade ? 'Live Unrealized' : 'Session Closed'}
          </span>
        </div>
      </div>

      {/* 4. TRADES */}
      <div className="term-panel p-3.5 flex flex-col justify-between">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Trades</span>
          <span className="text-[11px] font-mono text-slate-400">Max 1/Day</span>
        </div>
        <div className="mt-2">
          <span className="text-xl sm:text-2xl font-bold font-mono text-white tracking-tight">
            {tradesTaken} <span className="text-slate-500 font-normal">/</span> {maxTrades}
          </span>
          <span className="text-xs text-slate-400 block font-mono mt-0.5">
            {tradesTaken === 0 ? 'Quota Available' : 'Limit Reached'}
          </span>
        </div>
      </div>

      {/* 5. RISK USED */}
      <div className="term-panel p-3.5 flex flex-col justify-between">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Risk Used</span>
          <Shield className="w-3.5 h-3.5 text-rose-400" />
        </div>
        <div className="mt-2">
          <span className="text-xl sm:text-2xl font-bold font-mono text-white tracking-tight">
            {formatINR(riskUsed)}
          </span>
          <span className="text-xs text-slate-400 block font-mono mt-0.5">
            Limit: {formatINR(riskLimit)} (2%)
          </span>
        </div>
      </div>

      {/* 6. ACTIVE STRATEGY */}
      <div className="term-panel p-3.5 flex flex-col justify-between">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Active Strategy</span>
          <span className="text-[11px] font-mono text-indigo-300 font-semibold uppercase">{telemetry?.data_source || 'REAL KITE'}</span>
        </div>
        <div className="mt-2">
          <span className="text-base sm:text-lg font-extrabold font-mono text-white tracking-tight uppercase">
            {telemetry?.strategy || 'CPR'}
          </span>
          <span className="text-xs text-slate-400 block font-mono mt-0.5">
            {telemetry?.strategy_levels?.regime ? `Regime: ${telemetry.strategy_levels.regime}` : 'Intraday Mode'}
          </span>
        </div>
      </div>

      {/* 7. ALGORITHM STATE */}
      <div className="term-panel p-3.5 flex flex-col justify-between col-span-2 sm:col-span-1">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Engine State</span>
          <Activity className="w-3.5 h-3.5 text-indigo-400" />
        </div>
        <div className="mt-2">
          <span className={`text-xs sm:text-sm font-bold font-mono px-2 py-1 rounded border inline-block ${getStatusColor(algoState)}`}>
            {algoState}
          </span>
          <span className="text-xs text-slate-400 block font-mono mt-1">
            {telemetry?.market_status === 'OPEN' ? 'Market Live' : 'Market Closed'}
          </span>
        </div>
      </div>
    </div>
  );
}
