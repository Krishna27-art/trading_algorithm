import React, { useState, useEffect } from 'react';
import { getOrders } from '../api/positions';
import './TradesPage.css'; /* reuse same CSS */

const OrdersPage = () => {
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [statusFilter, setStatusFilter] = useState('all');

  useEffect(() => {
    fetchOrders();
    const interval = setInterval(fetchOrders, 30000);
    return () => clearInterval(interval);
  }, []);

  const fetchOrders = async () => {
    try {
      const data = await getOrders();
      setOrders(data?.orders || data || []);
      setError(null);
    } catch (err) {
      setError('Failed to load orders. Check your connection.');
    } finally {
      setLoading(false);
    }
  };

  const filtered = orders.filter(o => {
    if (statusFilter === 'all') return true;
    return (o.status || '').toLowerCase() === statusFilter;
  });

  const statusOptions = ['all', 'pending', 'complete', 'cancelled', 'rejected'];

  const statusColor = (s) => {
    const map = { complete: 'badge-closed', pending: 'badge-pending', cancelled: 'badge-cancelled', rejected: 'badge-rejected' };
    return map[(s || '').toLowerCase()] || 'badge-open';
  };

  return (
    <div className="trades-page">
      <div className="page-header">
        <div>
          <h1>Order Log</h1>
          <p className="subtitle">All strategy orders from the trade journal</p>
        </div>
        <div className="header-stats">
          <div className="stat-chip">
            <span className="stat-label">Total Orders</span>
            <span className="stat-value">{filtered.length}</span>
          </div>
        </div>
      </div>

      <div className="filter-bar">
        {statusOptions.map(f => (
          <button
            key={f}
            id={`order-filter-${f}`}
            className={`filter-btn${statusFilter === f ? ' active' : ''}`}
            onClick={() => setStatusFilter(f)}
          >
            {f.charAt(0).toUpperCase() + f.slice(1)}
          </button>
        ))}
        <button className="refresh-btn" onClick={fetchOrders}>↻ Refresh</button>
      </div>

      {loading && (
        <div className="loading-state">
          <div className="spinner" />
          <span>Loading orders...</span>
        </div>
      )}

      {error && <div className="error-banner">{error}</div>}

      {!loading && !error && filtered.length === 0 && (
        <div className="empty-state">
          <div className="empty-icon">📄</div>
          <p>No orders found. Connect Kite and run a strategy to populate the order log.</p>
        </div>
      )}

      {!loading && filtered.length > 0 && (
        <div className="trades-table-wrapper">
          <table className="trades-table">
            <thead>
              <tr>
                <th>Order ID</th>
                <th>Symbol</th>
                <th>Type</th>
                <th>Direction</th>
                <th>Qty</th>
                <th>Price</th>
                <th>Trigger</th>
                <th>Status</th>
                <th>Placed At</th>
                <th>Strategy</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((order, i) => (
                <tr key={order.order_id || i}>
                  <td className="trade-id">{order.order_id || '—'}</td>
                  <td className="symbol">{order.symbol || '—'}</td>
                  <td>{order.order_type || '—'}</td>
                  <td className={`direction ${order.direction === 'BUY' ? 'long' : 'short'}`}>
                    {order.direction === 'BUY' ? '▲ BUY' : '▼ SELL'}
                  </td>
                  <td>{order.quantity ?? '—'}</td>
                  <td>{order.price != null ? `₹${order.price.toLocaleString('en-IN')}` : 'MARKET'}</td>
                  <td>{order.trigger_price != null ? `₹${order.trigger_price.toLocaleString('en-IN')}` : '—'}</td>
                  <td>
                    <span className={`status-badge ${statusColor(order.status)}`}>
                      {(order.status || 'UNKNOWN').toUpperCase()}
                    </span>
                  </td>
                  <td>{order.placed_at ? new Date(order.placed_at).toLocaleTimeString() : '—'}</td>
                  <td>{order.strategy || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

export default OrdersPage;
