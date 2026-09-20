import React from 'react';
import { Clock, CheckCircle2, ChevronRight } from 'lucide-react';

export default function MarketSessionTimeline({ currentPhase }) {
  const steps = [
    { time: '09:15', label: 'Market Open', desc: 'Pre-market discovery', key: 'MARKET_OPEN' },
    { time: '09:15–09:45', label: 'Opening Range', desc: 'OR High/Low formation', key: 'ORB_BUILDING' },
    { time: '09:45', label: 'Breakout Monitoring', desc: '15m signal scanning', key: 'ENTRY_SCANNING' },
    { time: '13:30', label: 'Entry Cutoff', desc: 'No new positions', key: 'ENTRY_CLOSED' },
    { time: '14:30', label: 'Square Off', desc: 'Mandatory MIS exit', key: 'SQUARE_OFF' },
    { time: '15:10', label: 'Hard Cutoff', desc: 'Trading halted', key: 'MARKET_CLOSED' },
  ];

  const getActiveIndex = () => {
    switch (currentPhase) {
      case 'PRE_MARKET':
      case 'MARKET_OPEN':
        return 0;
      case 'ORB_BUILDING':
        return 1;
      case 'ENTRY_SCANNING':
        return 2;
      case 'ENTRY_CLOSED':
        return 3;
      case 'SQUARE_OFF':
        return 4;
      case 'MARKET_CLOSED':
        return 5;
      default:
        return 2;
    }
  };

  const activeIdx = getActiveIndex();

  return (
    <div className="term-panel p-4 space-y-3">
      {/* Header */}
      <div className="flex items-center justify-between pb-2 border-b border-white/[0.08]">
        <div className="flex items-center gap-2">
          <Clock className="w-4 h-4 text-indigo-400" />
          <h3 className="text-sm font-bold text-white uppercase tracking-wider">
            NSE Session Schedule Timeline
          </h3>
        </div>
        <span className="text-xs font-mono text-slate-400">Trading Window Gates</span>
      </div>

      {/* Stepper Flow */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2 pt-1">
        {steps.map((step, idx) => {
          const isPassed = idx < activeIdx;
          const isCurrent = idx === activeIdx;

          return (
            <div
              key={step.key}
              className={`p-2.5 rounded-lg border transition-all flex flex-col justify-between ${
                isCurrent
                  ? 'bg-indigo-600/20 border-indigo-500/50 shadow-md shadow-indigo-500/10'
                  : isPassed
                  ? 'bg-white/[0.02] border-white/5 opacity-70'
                  : 'bg-black/20 border-white/5 opacity-40'
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="text-xs font-mono font-bold text-slate-300">{step.time}</span>
                {isCurrent && <span className="w-2 h-2 rounded-full bg-indigo-400 animate-pulse" />}
                {isPassed && <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />}
              </div>

              <div className="mt-2">
                <p className={`text-xs font-bold ${isCurrent ? 'text-white font-extrabold' : 'text-slate-300'}`}>
                  {step.label}
                </p>
                <p className="text-[10px] text-slate-400 font-mono mt-0.5">{step.desc}</p>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
