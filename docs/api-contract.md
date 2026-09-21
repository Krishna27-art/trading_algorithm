# Trading Algorithm API Contract

Comprehensive technical specification of all backend endpoints consumed by the user interface.

---

## 1. Authentication & Session Status

### `GET /api/status`
Checks and validates the active Zerodha Kite Connect session.

- **Request**:
  - Headers: None required
  - Query Parameters: None
- **Response (`200 OK`)**:
  ```json
  {
    "authenticated": true,
    "status": "CONNECTED",
    "user": {
      "user_id": "AB1234",
      "user_name": "Trader Name",
      "login_time": "2026-09-21T09:15:00",
      "api_key": "sswg****"
    }
  }
  ```
- **Error Responses**:
  - `200 OK` with `"authenticated": false, "status": "DISCONNECTED"` when session file is absent or expired.
- **Frontend Consumer**:
  - [`TopNavbar.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/terminal/TopNavbar.jsx)
  - [`Dashboard.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/Dashboard.jsx)

---

## 2. Portfolio Margins

### `GET /api/margins`
Retrieves live available cash and margin balances from Zerodha Kite Connect.

- **Request**:
  - Query Parameters: None
- **Response (`200 OK`)**:
  ```json
  {
    "equity": {
      "enabled": true,
      "net": 1000000.0,
      "available": {
        "cash": 950000.0,
        "live_balance": 950000.0
      },
      "utilised": {
        "m2m_realised": 0.0,
        "m2m_unrealised": 0.0
      }
    }
  }
  ```
- **Error Responses**:
  - `200 OK` with `{}` when Kite is offline. Frontend renders `—` without fabricating default balances.
- **Frontend Consumer**:
  - [`TopKpiBar.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/terminal/TopKpiBar.jsx)

---

## 3. Broker Net Positions

### `GET /api/portfolio/positions`
Returns single-source-of-truth broker net positions from Zerodha Kite (or Paper Broker Simulator). Deduplicated strictly by `exchange:tradingsymbol:product`.

- **Request**:
  - Query Parameters: None
- **Response (`200 OK`)**:
  ```json
  {
    "status": "success",
    "broker": "LIVE",
    "authenticated": true,
    "count": 1,
    "total_unrealised_pnl": 1250.0,
    "total_realised_pnl": 0.0,
    "total_pnl": 1250.0,
    "positions": [
      {
        "position_id": "NSE:SBILIFE:MIS",
        "tradingsymbol": "SBILIFE",
        "exchange": "NSE",
        "product": "MIS",
        "quantity": 10,
        "average_price": 1740.0,
        "last_price": 1750.0,
        "pnl": 100.0,
        "unrealised_pnl": 100.0,
        "realised_pnl": 0.0,
        "stop_loss": 1720.0,
        "target": 1810.0,
        "trade_id": "TRD_SBILIFE_01"
      }
    ],
    "timestamp": "2026-09-21T09:45:00"
  }
  ```
- **Frontend Consumer**:
  - [`PositionsPage.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/terminal/PositionsPage.jsx)
  - [`ActiveTradeCard.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/terminal/ActiveTradeCard.jsx)

---

## 4. Trade Execution Journal

### `GET /api/strategy/trades`
Fetches historical trade execution records from the local SQLite database.

- **Request**:
  - Query Parameters: None
- **Response (`200 OK`)**:
  ```json
  {
    "trades": [
      {
        "trade_id": "TRD_20260921_01",
        "symbol": "NIFTY",
        "direction": "BUY",
        "entry_time": "2026-09-21T09:45:00",
        "entry_price": 24050.0,
        "exit_time": "2026-09-21T11:15:00",
        "exit_price": 24150.0,
        "quantity": 65,
        "pnl_gross": 6500.0,
        "pnl_net": 6380.0,
        "exit_reason": "PROFIT_TARGET"
      }
    ]
  }
  ```
- **Frontend Consumer**:
  - [`PositionsPage.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/terminal/PositionsPage.jsx) (Trade Journal Tab)

---

## 5. Order History

### `GET /api/strategy/orders`
Retrieves institutional order audit records.

- **Request**:
  - Query Parameters: None
- **Response (`200 OK`)**:
  ```json
  {
    "orders": [
      {
        "order_id": "ORD_20260921_01",
        "symbol": "SBILIFE",
        "direction": "BUY",
        "order_type": "LIMIT",
        "quantity": 10,
        "price": 1750.0,
        "status": "COMPLETE",
        "tag": "ALGO_PAPER",
        "created_at": "2026-09-21T09:45:00"
      }
    ]
  }
  ```
- **Frontend Consumer**:
  - [`OrdersPage.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/terminal/OrdersPage.jsx)

---

## 6. System Health

### `GET /api/system/health`
Monitors subsystem readiness using normalized enums.

- **Response (`200 OK`)**:
  ```json
  {
    "kite_api": "CONNECTED",
    "market_data": "CONNECTED",
    "database": "CONNECTED",
    "strategy_engine": "RUNNING",
    "risk_engine": "READY",
    "order_manager": "READY",
    "active_broker": "PAPER",
    "overall_status": "READY",
    "timestamp": "2026-09-21T09:45:00"
  }
  ```
- **Normalized Status Enum Values**:
  - `CONNECTED`
  - `DISCONNECTED`
  - `RUNNING`
  - `STOPPED`
  - `READY`
  - `ERROR`
  - `UNKNOWN`
- **Frontend Consumer**:
  - [`SystemHealthBar.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/terminal/SystemHealthBar.jsx)
  - [`SystemPage.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/terminal/SystemPage.jsx)

---

## 7. Unified Multi-Strategy Live Research

### `GET /api/research/live`
Ranks the NIFTY 50 universe with real Kite quotes and runs the actual `IntradayORBStrategy`, `CPRRegimeBreakoutStrategy`, and `BufferedDualEMAStrategy` engines to generate multi-strategy predictions, consensus direction, and key market insights.

- **Query Parameters**:
  - `top_n` (int, default: 10): Number of top-ranked candidates to return
  - `force_refresh` (bool, default: false): Invalidate server cache
- **Response (`200 OK`)**:
  ```json
  {
    "status": "success",
    "data_source": "REAL_KITE",
    "timestamp": "2026-09-21T09:45:00",
    "market_status": "OPEN",
    "scanned_count": 50,
    "returned_count": 10,
    "candidates": [
      {
        "rank": 1,
        "symbol": "SBILIFE",
        "ltp": 1750.0,
        "momentum_score": 37.0,
        "universe_bias": "LONG",
        "predictions": {
          "orb": {
            "status": "LONG_BREAKOUT",
            "direction": "LONG",
            "entry": 1750.0,
            "stop_loss": 1720.0,
            "target": 1810.0,
            "reason": "Price broke above 30m ORB High with VWAP confirmation",
            "levels": {
              "orb_high": 1740.0,
              "orb_low": 1715.0,
              "orb_width": 25.0,
              "is_valid_volatility": true
            }
          },
          "cpr": {
            "status": "BULLISH_EXPANSION",
            "direction": "LONG",
            "entry": 1750.0,
            "stop_loss": 1730.0,
            "target": 1790.0,
            "reason": "Price above TC during NARROW regime",
            "levels": {
              "pivot": 1730.0,
              "bottom_central": 1725.0,
              "top_central": 1735.0,
              "regime": "NARROW"
            }
          },
          "dual_ema": {
            "status": "TRENDING_LONG",
            "direction": "LONG",
            "entry": 1750.0,
            "stop_loss": 1725.0,
            "target": 1795.0,
            "reason": "EMA9 above EMA21 with buffer & price above SMA200",
            "levels": {
              "ema_fast": 1745.0,
              "ema_slow": 1735.0,
              "sma_trend": 1710.0,
              "atr_14": 18.0
            }
          }
        },
        "consensus": {
          "direction": "LONG",
          "agreeing_strategies": 3,
          "total_strategies": 3,
          "label": "UNANIMOUS LONG"
        }
      }
    ],
    "key_insights": {
      "top_long": { "symbol": "SBILIFE", "..." : "..." },
      "top_short": { "symbol": "INFY", "..." : "..." },
      "strongest_consensus": { "symbol": "SBILIFE", "..." : "..." },
      "divergent_signals": []
    }
  }
  ```
- **Error Response**:
  - `status: "AUTH_REQUIRED"` if Zerodha Kite Connect is unauthenticated.
- **Frontend Consumer**:
  - [`ResearchPredictionTable.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/terminal/ResearchPredictionTable.jsx)
  - [`KeyInsightsPanel.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/terminal/KeyInsightsPanel.jsx)

---

## 8. Multi-Strategy Backtesting

### `GET/POST /api/research/backtest`
Executes real event-driven backtesting across all 3 strategies (`ORB`, `CPR`, and `Dual-EMA`) over validated historical candles (default: 180 days).

- **Query Parameters**:
  - `days` (int, default: 180)
  - `symbol` (str, default: "NIFTY")
- **Response (`200 OK`)**:
  ```json
  {
    "days": 180,
    "symbol": "NIFTY",
    "data_source": "REAL_KITE",
    "strategies": {
      "orb": {
        "total_trades": 158,
        "long_trades": 80,
        "short_trades": 78,
        "winning_trades": 61,
        "losing_trades": 97,
        "win_rate_pct": 38.6,
        "gross_pnl": 85000.0,
        "total_transaction_costs": 31360.31,
        "net_pnl": 53639.69,
        "profit_factor": 1.07,
        "sharpe_ratio": 0.85,
        "cagr_pct": 5.4,
        "max_drawdown_pct": 12.1,
        "expectancy_rupees": 339.49,
        "long_win_rate": 40.0,
        "short_win_rate": 37.2,
        "long_net_pnl": 32000.0,
        "short_net_pnl": 21639.69,
        "yearly_returns": { "2025": 5.4 }
      },
      "cpr": {
        "total_trades": 57,
        "winning_trades": 33,
        "win_rate_pct": 57.9,
        "profit_factor": 1.39,
        "net_pnl": 115075.84,
        "...": "..."
      },
      "dual_ema": {
        "total_trades": 177,
        "winning_trades": 75,
        "win_rate_pct": 42.4,
        "profit_factor": 1.03,
        "net_pnl": 27711.18,
        "...": "..."
      }
    },
    "comparison": [
      {
        "strategy": "Central Pivot Range (CPR) Regime",
        "strategy_id": "cpr",
        "trades": 57,
        "win_rate": 57.9,
        "profit_factor": 1.39,
        "sharpe": 1.56,
        "max_drawdown": 8.4,
        "net_pnl": 115075.84,
        "state": "ACTIVE"
      },
      {
        "strategy": "30-Min Volatility-Filtered ORB",
        "strategy_id": "orb",
        "trades": 158,
        "win_rate": 38.6,
        "profit_factor": 1.07,
        "sharpe": 0.85,
        "max_drawdown": 12.1,
        "net_pnl": 53639.69,
        "state": "STANDBY"
      },
      {
        "strategy": "Adaptive Dual-EMA Trend System",
        "strategy_id": "dual_ema",
        "trades": 177,
        "win_rate": 42.4,
        "profit_factor": 1.03,
        "sharpe": 0.92,
        "max_drawdown": 14.2,
        "net_pnl": 27711.18,
        "state": "STANDBY"
      }
    ]
  }
  ```
- **Frontend Consumer**:
  - [`MultiStrategyBacktestPanel.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/terminal/MultiStrategyBacktestPanel.jsx)
  - [`PerformancePage.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/terminal/PerformancePage.jsx)

---

## 9. Order Placement

### `POST /api/orders/place`
Places an entry order through pre-trade risk controls (loss cap, daily trade count, position limits).

- **Headers**:
  - `X-Shared-Secret`: Required
  - `Content-Type`: `application/json`
- **Request Body**:
  ```json
  {
    "symbol": "SBILIFE",
    "direction": "BUY",
    "order_type": "LIMIT",
    "price": 1750.0,
    "quantity": 10,
    "mode": "PAPER"
  }
  ```
- **Response (`200 OK`)**:
  ```json
  {
    "success": true,
    "order": {
      "order_id": "ORD_17899600",
      "symbol": "SBILIFE",
      "direction": "BUY",
      "quantity": 10,
      "price": 1750.0,
      "status": "COMPLETE"
    }
  }
  ```
- **Error Responses**:
  - `401 Unauthorized`: Missing or invalid `X-Shared-Secret`
  - `403 Forbidden`: Pre-trade risk gate rejection (e.g. daily loss limit hit)
  - `400 Bad Request`: Invalid parameters or Kite session offline for live orders
- **Frontend Consumer**:
  - [`QuickOrderModal.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/terminal/QuickOrderModal.jsx)
  - [`OrderExecutionPanel.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/terminal/OrderExecutionPanel.jsx)

---

## 10. Square Off Position

### `POST /api/orders/exit`
Squares off any active open position for the specified symbol immediately at market price.

- **Headers**:
  - `X-Shared-Secret`: Required
  - `Content-Type`: `application/json`
- **Request Body**:
  ```json
  {
    "symbol": "SBILIFE",
    "mode": "PAPER"
  }
  ```
- **Response (`200 OK`)**:
  ```json
  {
    "success": true,
    "message": "Closed position on SBILIFE",
    "order": {
      "order_id": "EXIT_17899601",
      "symbol": "SBILIFE",
      "direction": "SELL",
      "quantity": 10,
      "status": "COMPLETE"
    }
  }
  ```
- **Frontend Consumer**:
  - [`PositionsPage.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/terminal/PositionsPage.jsx)
  - [`ActiveTradeCard.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/terminal/ActiveTradeCard.jsx)
  - [`Dashboard.jsx`](file:///Users/pandu/Desktop/trading%20algorithm/frontend/src/components/Dashboard.jsx)
