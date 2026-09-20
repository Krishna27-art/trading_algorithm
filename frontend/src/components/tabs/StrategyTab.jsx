import React, { useEffect, useState } from 'react';
import { Cpu, ShieldCheck, Play, Sliders, CheckCircle2, TrendingUp, AlertTriangle, RefreshCw, BarChart2, Check, ExternalLink } from 'lucide-react';

export default function StrategyTab() {
  const [strategyState, setStrategyState] = useState(null);
  const [backtestReport, setBacktestReport] = useState(null);
  const [loadingBacktest, setLoadingBacktest] = useState(false);
  const [trades, setTrades] = useState([]);

  const fetchState = async () => {
    try {
      const res = await fetch('/api/strategy/state');
      if (res.ok) {
        const data = await res.json();
        setStrategyState(data);
      }
    } catch (e) {
      console.error(e);
    }
  };

  const fetchTrades = async () => {
    try {
      const res = await fetch('/api/strategy/trades');
      if (res.ok) {
        const data = await res.json();
        setTrades(data.trades || []);
      }
    } catch (e) {
      console.error(e);
    }
  };

  const runBacktest = async () => {
    setLoadingBacktest(true);
    try {
      const res = await fetch('/api/strategy/backtest', { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        setBacktestReport(data.report);
      }
    } catch (e) {
      console.error(e);
    } finally {
      setLoadingBacktest(false);
    }
  };

  useEffect(() => {
    fetchState();
    fetchTrades();
  }, []);

  const formatINR = (val) => {
    if (val === undefined || val === null) return '₹0.00';
    return new Intl.NumberFormat('en-IN', {
      style: 'currency',
      currency: 'INR',
      maximumFractionDigits: 2,
    }).format(val);
  };

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Strategy Header Banner */}
      <div className="glass-panel p-6 border-indigo-500/30 flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 rounded-xl bg-gradient-to-tr from-indigo-600 to-emerald-500 flex items-center justify-center text-white shadow-lg shadow-indigo-500/20">
            <Cpu className="w-6 h-6" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-lg font-bold text-white tracking-tight">
                30-Minute Volatility-Filtered ORB
              </h2>
              <span className="badge badge-emerald">
                {strategyState?.is_paper_trading ? 'Paper Trading Simulator' : 'Live Gateway'}
              </span>
            </div>
            <p className="text-xs text-slate-400 mt-0.5">
              Instrument: <span className="font-mono text-indigo-300 font-semibold">{strategyState?.symbol || 'NIFTY'}</span> (Lot Size: {strategyState?.lot_size || 25}) • Mode: <span className="text-emerald-400 font-semibold">{strategyState?.active_broker || 'PAPER'}</span>
            </p>
          </div>
        </div>

        <button
          onClick={runBacktest}
          disabled={loadingBacktest}
          className="btn-primary text-xs py-2.5 px-4 flex items-center gap-2"
        >
          {loadingBacktest ? (
            <>
              <RefreshCw className="w-4 h-4 animate-spin" />
              <span>Running Backtest...</span>
            </>
          ) : (
            <>
              <BarChart2 className="w-4 h-4" />
              <span>Run Event-Driven Backtest</span>
            </>
          )}
        </button>
      </div>

      {/* Rules & Architecture Overview */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
        {/* Timing Window Card */}
        <div className="glass-panel p-5 space-y-3">
          <div className="flex items-center gap-2 text-indigo-400 pb-2 border-b border-white/5">
            <Sliders className="w-4 h-4" />
            <h3 className="text-xs font-bold uppercase tracking-wider text-slate-200">Session Windows</h3>
          </div>
          <div className="space-y-2 text-xs font-mono">
            <div className="flex justify-between">
              <span className="text-slate-400">Opening Range:</span>
              <span className="text-slate-200">09:15 – 09:45 IST</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Entry Scanning:</span>
              <span className="text-slate-200">09:45 – 13:30 IST</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Square-Off:</span>
              <span className="text-rose-300 font-bold">14:30 IST</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Hard Cutoff:</span>
              <span className="text-rose-400">15:10 IST</span>
            </div>
          </div>
        </div>

        {/* Volatility & Signal Rules */}
        <div className="glass-panel p-5 space-y-3">
          <div className="flex items-center gap-2 text-emerald-400 pb-2 border-b border-white/5">
            <CheckCircle2 className="w-4 h-4" />
            <h3 className="text-xs font-bold uppercase tracking-wider text-slate-200">Signal & Confirmation</h3>
          </div>
          <div className="space-y-2 text-xs font-mono">
            <div className="flex justify-between">
              <span className="text-slate-400">Candle Timeframe:</span>
              <span className="text-emerald-300">15-Minute Bars</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Volatility Cutoff:</span>
              <span className="text-emerald-300">Width &ge; 40.0 pts</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">VWAP Confirmation:</span>
              <span className="text-emerald-300">Strictly Enforced</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Max Risk Cap:</span>
              <span className="text-indigo-300">80 pts (if Width &gt; 120)</span>
            </div>
          </div>
        </div>

        {/* Risk Management Limits */}
        <div className="glass-panel p-5 space-y-3">
          <div className="flex items-center gap-2 text-amber-400 pb-2 border-b border-white/5">
            <ShieldCheck className="w-4 h-4" />
            <h3 className="text-xs font-bold uppercase tracking-wider text-slate-200">Risk Safeguards</h3>
          </div>
          <div className="space-y-2 text-xs font-mono">
            <div className="flex justify-between">
              <span className="text-slate-400">Risk per Trade:</span>
              <span className="text-slate-200">1.0% Capital</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Reward-to-Risk:</span>
              <span className="text-slate-200">2.0 : 1.0 Payoff</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Breakeven Trailing:</span>
              <span className="text-emerald-400 font-bold">+1.0R Trigger</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Daily Loss Circuit:</span>
              <span className="text-rose-400 font-bold">2.0% Kill-Switch</span>
            </div>
          </div>
        </div>
      </div>

      {/* Backtest Results Display */}
      {backtestReport && (
        <div className="glass-panel p-6 space-y-5 animate-fade-in border-emerald-500/20">
          <div className="flex items-center justify-between pb-3 border-b border-white/5">
            <div>
              <h3 className="text-base font-bold text-white flex items-center gap-2">
                <BarChart2 className="w-5 h-5 text-emerald-400" />
                Event-Driven Backtest Performance (Oct 2024 SEBI Frictions Deducted)
              </h3>
              <p className="text-xs text-slate-400">Historical simulation over 180 trading sessions with zero look-ahead bias</p>
            </div>
            <span className="badge badge-emerald">Verified Zero Look-Ahead</span>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <div className="p-3.5 rounded-xl bg-white/5 border border-white/5">
              <span className="text-[11px] text-slate-400 block">Total Net P&L</span>
              <span className="text-lg font-bold font-mono text-emerald-400">{formatINR(backtestReport.net_pnl)}</span>
            </div>
            <div className="p-3.5 rounded-xl bg-white/5 border border-white/5">
              <span className="text-[11px] text-slate-400 block">Win Rate</span>
              <span className="text-lg font-bold font-mono text-white">{backtestReport.win_rate_pct}%</span>
              <span className="text-[10px] text-slate-400">{backtestReport.winning_trades}W / {backtestReport.losing_trades}L</span>
            </div>
            <div className="p-3.5 rounded-xl bg-white/5 border border-white/5">
              <span className="text-[11px] text-slate-400 block">Profit Factor</span>
              <span className="text-lg font-bold font-mono text-indigo-300">{backtestReport.profit_factor}</span>
            </div>
            <div className="p-3.5 rounded-xl bg-white/5 border border-white/5">
              <span className="text-[11px] text-slate-400 block">Sharpe Ratio</span>
              <span className="text-lg font-bold font-mono text-cyan-300">{backtestReport.sharpe_ratio}</span>
            </div>
            <div className="p-3.5 rounded-xl bg-white/5 border border-white/5">
              <span className="text-[11px] text-slate-400 block">CAGR</span>
              <span className="text-lg font-bold font-mono text-white">{backtestReport.cagr_pct}%</span>
            </div>
            <div className="p-3.5 rounded-xl bg-white/5 border border-white/5">
              <span className="text-[11px] text-slate-400 block">Max Drawdown</span>
              <span className="text-lg font-bold font-mono text-rose-400">-{backtestReport.max_drawdown_pct}%</span>
            </div>
            <div className="p-3.5 rounded-xl bg-white/5 border border-white/5">
              <span className="text-[11px] text-slate-400 block">Total Statutory Taxes</span>
              <span className="text-lg font-bold font-mono text-slate-300">{formatINR(backtestReport.total_transaction_costs)}</span>
            </div>
            <div className="p-3.5 rounded-xl bg-white/5 border border-white/5">
              <span className="text-[11px] text-slate-400 block">Avg R / Trade</span>
              <span className="text-lg font-bold font-mono text-white">{backtestReport.avg_r_multiple}R</span>
            </div>
          </div>

          <div className="p-4 rounded-xl bg-slate-900/60 border border-white/5 flex flex-col sm:flex-row justify-between gap-4 text-xs font-mono">
            <div>
              <span className="text-slate-400">Long Trades Win Rate: </span>
              <span className="text-slate-200">{backtestReport.long_win_rate}% ({formatINR(backtestReport.long_net_pnl)})</span>
            </div>
            <div>
              <span className="text-slate-400">Short Trades Win Rate: </span>
              <span className="text-slate-200">{backtestReport.short_win_rate}% ({formatINR(backtestReport.short_net_pnl)})</span>
            </div>
            <div>
              <span className="text-slate-400">Max Consecutive Losses: </span>
              <span className="text-rose-400">{backtestReport.max_consecutive_losses} Trades</span>
            </div>
          </div>
        </div>
      )}

      {/* Trade Journal & SQLite Audit Trail */}
      <div className="glass-panel p-6 space-y-4">
        <div className="flex items-center justify-between pb-3 border-b border-white/5">
          <div>
            <h3 className="text-base font-bold text-white">Trade Journal & Audit Log (SQLite)</h3>
            <p className="text-xs text-slate-400">Recorded paper executions and historical journal entries</p>
          </div>
          <button onClick={fetchTrades} className="btn-secondary text-xs">
            <RefreshCw className="w-3.5 h-3.5" />
            <span>Refresh Journal</span>
          </button>
        </div>

        {trades.length === 0 ? (
          <div className="py-8 text-center text-xs text-slate-400">
            No active trade records found in SQLite journal. Run paper simulation or click "Run Event-Driven Backtest" above.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left font-mono text-xs">
              <thead className="text-[11px] text-slate-400 uppercase border-b border-white/10 pb-2">
                <tr>
                  <th className="py-2.5">Trade ID</th>
                  <th>Symbol</th>
                  <th>Action</th>
                  <th>Quantity</th>
                  <th>Entry Price</th>
                  <th>Exit Price</th>
                  <th>Net P&L</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/5 text-slate-200">
                {trades.slice(0, 10).map((t) => (
                  <tr key={t.trade_id} className="hover:bg-white/5 transition-colors">
                    <td className="py-2.5 text-indigo-400 font-semibold">{t.trade_id}</td>
                    <td>{t.symbol}</td>
                    <td>
                      <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                        t.direction === 'BUY' ? 'bg-emerald-500/20 text-emerald-300' : 'bg-rose-500/20 text-rose-300'
                      }`}>
                        {t.direction}
                      </span>
                    </td>
                    <td>{t.quantity}</td>
                    <td>₹{t.entry_price?.toFixed(2)}</td>
                    <td>{t.exit_price ? `₹${t.exit_price.toFixed(2)}` : 'OPEN'}</td>
                    <td className={t.pnl_net >= 0 ? 'text-emerald-400 font-bold' : 'text-rose-400 font-bold'}>
                      {formatINR(t.pnl_net)}
                    </td>
                    <td className="text-slate-400">{t.exit_reason || t.notes || 'IN_FLIGHT'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
