# Institutional NSE Intraday Algorithmic Trading System
### 30-Minute Volatility-Filtered Opening Range Breakout (ORB)

A production-grade algorithmic trading system for the **National Stock Exchange of India (NSE)**, calibrated specifically for **NIFTY Index Futures** and liquid mega-cap equities based on empirical microstructure research.

---

## 📐 Quantitative Edge & Strategy Specification

The strategy harvests directional edges during the morning price discovery window while strictly filtering out low-volatility whipsaws and anchoring direction to volume-weighted fair value.

### 1. Mathematical Formulations & Rules
- **Opening Range (09:15 – 09:45 IST)**:
  $$\text{OR}_{\text{High}} = \max_{t \in [09:15, 09:45]} (\text{High}_t)$$
  $$\text{OR}_{\text{Low}} = \min_{t \in [09:15, 09:45]} (\text{Low}_t)$$
  $$\text{OR}_{\text{Width}} = \text{OR}_{\text{High}} - \text{OR}_{\text{Low}}$$
- **Volatility Filter**:
  - For NIFTY Index: $\text{OR}_{\text{Width}} \ge 40.0$ index points. If width $< 40$, skip all entries for the day (avoids compressed/choppy regimes).
  - Risk distance capping: If $\text{OR}_{\text{Width}} > 120$ index points, cap effective risk distance at $80.0$ points ($R_{\text{trade}} = \min(R_{\text{trade}}, 80.0)$).
- **Session VWAP Confirmation**:
  $$\text{VWAP}_t = \frac{\sum_{i=1}^t \text{Price}_{\text{typ}, i} \times \text{Volume}_i}{\sum_{i=1}^t \text{Volume}_i}, \quad \text{Price}_{\text{typ}} = \frac{\text{High} + \text{Low} + \text{Close}}{3}$$
- **Long Entry**:
  - Completed 15-min bar closes strictly above $\text{OR}_{\text{High}}$ **AND** strictly above session $\text{VWAP}$.
  - Initial Stop Loss = $\text{OR}_{\text{Low}}$.
  - Target = $\text{Entry} + 2.0 \times R_{\text{trade}}$ (2:1 reward-to-risk).
  - Breakeven Trailing: Once favorable price reaches $+1.0 \times R_{\text{trade}}$, move Stop Loss to $\text{Entry}$ (eliminates residual downside).
- **Short Entry**:
  - Completed 15-min bar closes strictly below $\text{OR}_{\text{Low}}$ **AND** strictly below session $\text{VWAP}$.
  - Initial Stop Loss = $\text{OR}_{\text{High}}$.
  - Target = $\text{Entry} - 2.0 \times R_{\text{trade}}$ (2:1 reward-to-risk).
  - Breakeven Trailing: Once favorable price reaches $-1.0 \times R_{\text{trade}}$, move Stop Loss to $\text{Entry}$.
- **Strict Time Rules**:
  - Entry Window: **09:45 to 13:30 IST**.
  - Mandatory Square-Off: **14:30 IST** (cancels all resting orders and squares off open positions to eliminate late-session liquidity traps and avoid broker auto-square-off charges).
  - Hard Cutoff: **15:10 IST**.
  - Strictly **Max 1 trade per instrument per day**.
  - Never carry positions overnight.

---

## 🛡️ Risk Management & Portfolio Circuit-Breaker

- **Fixed Fractional Sizing**: Risks maximum 1.0% of liquid trading capital per trade:
  $$\text{Quantity} = \left\lfloor \frac{\text{Capital} \times 0.01}{R_{\text{trade}}} \right\rfloor$$
  Rounded down to exchange contract lot size (25 for NIFTY futures).
- **Daily Kill-Switch Circuit-Breaker**: If cumulative intraday losses reach **2.0% of capital**, a hard kill-switch is engaged:
  1. Closes open positions immediately.
  2. Cancels all resting limit and stop orders across broker gateways.
  3. Halts trading until the opening bell of the subsequent trading session.
- **Anti-Martingale**: Never averages down or adds to a losing position.

---

## 🏛️ October 2024 Revised SEBI Statutory Cost Schedule

Backtesting and live tracking explicitly incorporate all real-world exchange and statutory frictions:

| Cost Component | NIFTY Index Futures (MIS) | Equity Intraday (MIS) |
|---|---|---|
| **Brokerage** | Flat ₹20 per executed order | 0.03% or ₹20 (whichever lower) |
| **STT** | 0.02% on sell turnover (Revised Oct 2024) | 0.025% on sell turnover |
| **Exchange Charges** | 0.00190% on aggregate turnover | 0.00325% on aggregate turnover |
| **GST** | 18% on (Brokerage + Txn Charges + SEBI) | 18% on (Brokerage + Txn Charges + SEBI) |
| **SEBI Turnover Charges** | ₹10 per crore (0.0001%) | ₹10 per crore (0.0001%) |
| **Stamp Duty** | 0.002% on buy turnover | 0.003% on buy turnover |
| **Slippage Deduction** | 0.50 index points per round-trip | 0.02% or 1 tick |

---

## 📂 Modular Architecture

```
trading algorithm/
├── config/
│   └── settings.py          # Pydantic BaseSettings, risk rules, schedule
├── data/
│   ├── candle_aggregator.py # Real-time tick to 15m candle & VWAP aggregator
│   ├── historical_loader.py # OHLCV data loader & high-fidelity synthetic market generator
│   └── market_calendar.py   # NSE session phases & holiday calendar
├── indicators/
│   ├── orb.py               # 09:15-09:45 ORB calculator & volatility filters
│   ├── vwap.py              # Session-anchored Volume-Weighted Average Price
│   └── atr.py               # Average True Range for equity normalization
├── strategy/
│   ├── base_strategy.py     # Base abstract strategy interface & signals
│   └── orb_strategy.py      # Production 30-Minute ORB Strategy
├── risk/
│   ├── position_sizer.py    # Fractional 1% sizing with contract lot rounding & risk cap
│   ├── risk_manager.py      # Pre-trade checks & 2% daily loss kill-switch
│   └── transaction_costs.py # Exact Oct 2024 SEBI statutory cost model
├── broker/
│   ├── base_broker.py       # Abstract broker interface
│   ├── paper_broker.py      # Realistic paper execution simulator (DEFAULT)
│   ├── kite_adapter.py      # Zerodha Kite Connect v3 adapter
│   └── dhan_adapter.py      # DhanHQ API v2 adapter
├── execution/
│   ├── order_manager.py     # Order state machine (SUBMITTED, OPEN, FILLED, CANCELLED)
│   └── execution_engine.py  # Real-time event orchestrator & time-based square-off
├── portfolio/
│   └── portfolio_manager.py # Capital, daily P&L, position tracking & circuit breaker
├── database/
│   ├── db.py                # SQLite database manager
│   └── models.py            # TradeRecord, OrderRecord schemas
├── backtest/
│   ├── event_engine.py      # Event-driven backtester (zero look-ahead bias)
│   ├── performance.py       # Performance analytics (CAGR, Sharpe, Drawdown, Expectancy)
│   └── walk_forward.py      # 70/30 In-sample vs Out-of-sample walk-forward validator
├── monitoring/
│   ├── cli_monitor.py       # Live terminal telemetry dashboard
│   └── logger.py            # Structured logging framework
├── tests/
│   ├── test_indicators.py
│   ├── test_orb_strategy.py
│   ├── test_risk_manager.py
│   ├── test_transaction_costs.py
│   └── test_backtester.py
├── backend/                 # FastAPI REST API
├── frontend/                # React + Vite dark mode dashboard
├── run_algo.py              # CLI runner (backtest, walk-forward, paper, live)
└── package.json
```

---

## ⚡ Quickstart & Usage Commands

### 1. Run Event-Driven Backtest (CLI)
```bash
source .venv/bin/activate
python run_algo.py --mode backtest
```
Reports **Net P&L, CAGR, Win Rate, Profit Factor, Sharpe Ratio, Maximum Drawdown, Average R, Expectancy, Consecutive Losses, Long vs Short breakdown, and Transaction-Cost impact**.

### 2. Run Walk-Forward Validation (CLI)
```bash
python run_algo.py --mode walkforward
```
Tests in-sample (training 70%) vs out-of-sample (testing 30%) stability without parameter snooping.

### 3. Run Live Paper-Trading Simulation (CLI)
```bash
python run_algo.py --mode paper
```
Runs a simulated trading day with real-time ASCII telemetry dashboard and saves executed trades to SQLite.

### 4. Run Full-Stack Web Dashboard (React + FastAPI)
```bash
npm run dev
```
Open **[http://localhost:5173](http://localhost:5173)** to access the dark-mode dashboard with:
- Zerodha Kite Connect login
- User Profile tab (User Name, User ID, Products, Exchanges)
- Margins & Funds breakdown
- 30-Min ORB strategy state, live backtest trigger, and SQLite trade journal.

### 5. Run Unit & Integration Test Suite
```bash
pytest -v
```
All 14 tests covering indicators, strategies, risk checks, SEBI costs, and backtesting pass with 100% success.
