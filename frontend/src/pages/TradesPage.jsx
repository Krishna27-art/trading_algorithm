import React, { useState, useEffect } from 'react';
import { getTrades } from '../api/positions';
import './TradesPage.css';

const TradesPage = () => {
  const [trades, setTrades] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState('all'); // 'all' | 'open' | 'closed'

  useEffect(() => {
    fetchTrades();
    const interval = setInterval(fetchTrades, 30000);
    return () => clearInterval(interval);
  }, []);

  const fetchTrades = async () => {
    try {
      const data = await getTrades();
      setTrades(data?.trades || data || []);
      setError(null);
    } catch (err) {
      setError('Failed to load trades. Check your connection.');
    } finally {
      setLoading(false);
    }
  };

  const filtered = trades.filter(t => {
    if (filter === 'open') return !t.exit_price && !t.exit_time;
    if (filter === 'closed') return t.exit_price || t.exit_time;
    return true;
  });

  const totalPnl = filtered
    .filter(t => t.pnl_net !== null && t.pnl_net !== undefined)
    .reduce((sum, t) => sum + (t.pnl_net || 0), 0);

  const formatPnl = (val) => {
    if (val === null || val === undefined) return '—';
    const sign = val >= 0 ? '+' : '';
    return `${sign}₹${Math.abs(val).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
  };

  const pnlClass = (val) => {
    if (val === null || val === undefined) return '';
    return val >= 0 ? 'pnl-positive' : 'pnl-negative';
  };

  return (
    <div className="trades-page">
      <div className="page-header">
        <div>
          <h1>Trade Journal</h1>
          <p className="subtitle">All backtest and live strategy trade records</p>
        </div>
        <div className="header-stats">
          <div className="stat-chip">
            <span className="stat-label">Total Trades</span>
            <span className="stat-value">{filtered.length}</span>
          </div>
          <div className={`stat-chip ${pnlClass(totalPnl)}`}>
            <span className="stat-label">Net P&amp;L</span>
            <span className="stat-value">{formatPnl(totalPnl)}</span>
          </div>
        </div>
      </div>

      <div className="filter-bar">
        {['all', 'open', 'closed'].map(f => (
          <button
            key={f}
            id={`filter-${f}`}
            className={`filter-btn${filter === f ? ' active' : ''}`}
            onClick={() => setFilter(f)}
          >
            {f.charAt(0).toUpperCase() + f.slice(1)}
          </button>
        ))}
        <button className="refresh-btn" onClick={fetchTrades}>↻ Refresh</button>
      </div>

      {loading && (
        <div className="loading-state">
          <div className="spinner" />
          <span>Loading trades...</span>
        </div>
      )}

      {error && <div className="error-banner">{error}</div>}

      {!loading && !error && filtered.length === 0 && (
        <div className="empty-state">
          <div className="empty-icon">📋</div>
          <p>No trades found. Run a backtest or connect Kite to populate the trade journal.</p>
        </div>
      )}

      {!loading && filtered.length > 0 && (
        <div className="trades-table-wrapper">
          <table className="trades-table">
            <thead>
              <tr>
                <th>Trade ID</th>
                <th>Symbol</th>
                <th>Direction</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>Qty</th>
                <th>Gross P&amp;L</th>
                <th>Costs</th>
                <th>Net P&amp;L</th>
                <th>R-Multiple</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((trade, i) => {
                const isOpen = !trade.exit_price && !trade.exit_time;
                return (
                  <tr key={trade.trade_id || i} className={isOpen ? 'row-open' : 'row-closed'}>
                    <td className="trade-id">{trade.trade_id || '—'}</td>
                    <td className="symbol">{trade.symbol}</td>
                    <td className={`direction ${trade.direction === 'BUY' ? 'long' : 'short'}`}>
                      {trade.direction === 'BUY' ? '▲ LONG' : '▼ SHORT'}
                    </td>
                    <td>₹{trade.entry_price?.toLocaleString('en-IN')}</td>
                    <td>{trade.exit_price ? `₹${trade.exit_price.toLocaleString('en-IN')}` : '—'}</td>
                    <td>{trade.quantity ?? '—'}</td>
                    <td className={pnlClass(trade.pnl_gross)}>{formatPnl(trade.pnl_gross)}</td>
                    <td className="costs">{trade.total_costs != null ? `₹${trade.total_costs.toFixed(2)}` : '—'}</td>
                    <td className={`pnl-net ${pnlClass(trade.pnl_net)}`}>{formatPnl(trade.pnl_net)}</td>
                    <td className={pnlClass(trade.r_multiple)}>{trade.r_multiple != null ? `${trade.r_multiple}R` : '—'}</td>
                    <td>
                      <span className={`status-badge ${isOpen ? 'badge-open' : 'badge-closed'}`}>
                        {isOpen ? 'OPEN' : trade.exit_reason || 'CLOSED'}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

export default TradesPage;
