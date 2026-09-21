# Institutional NSE Intraday Algorithmic Trading System
### Multi-Strategy Quantitative Engine with Rolling Walk-Forward & Universe Scanner

A production-grade algorithmic trading framework for the **National Stock Exchange of India (NSE)**, calibrated specifically for **NIFTY Index Futures** and liquid mega-cap equities based on empirical market microstructure research.

---

## 📐 Quantitative Strategies & Mathematical Formulations

The engine provides a pluggable strategy architecture (`BaseStrategy`) supporting three distinct edge paradigms:

### 1. 30-Minute Volatility-Filtered Opening Range Breakout (`orb`)
Harveys morning price discovery momentum while strictly filtering out low-volatility whipsaws and anchoring directional entries to volume-weighted fair value.
- **Opening Range (09:15 – 09:45 IST)**:
  $$\text{OR}_{\text{High}} = \max_{t \in [09:15, 09:45]} (\text{High}_t), \quad \text{OR}_{\text{Low}} = \min_{t \in [09:15, 09:45]} (\text{Low}_t)$$
  $$\text{OR}_{\text{Width}} = \text{OR}_{\text{High}} - \text{OR}_{\text{Low}}$$
- **Volatility Filter**:
  - Requires $\text{OR}_{\text{Width}} \ge 40.0$ index points on NIFTY. Width $< 40$ suppresses entries for the entire day (regime filter).
  - Risk capping: If $\text{OR}_{\text{Width}} > 120$ index points, maximum risk distance is capped at $80.0$ points ($R_{\text{trade}} = \min(R_{\text{trade}}, 80.0)$).
- **Session VWAP Confirmation**:
  $$\text{VWAP}_t = \frac{\sum_{i=1}^t \text{Price}_{\text{typ}, i} \times \text{Volume}_i}{\sum_{i=1}^t \text{Volume}_i}, \quad \text{Price}_{\text{typ}} = \frac{\text{High} + \text{Low} + \text{Close}}{3}$$
- **Long Entry**: Bar closes $> \text{OR}_{\text{High}}$ and $> \text{VWAP}$. Initial SL = $\text{OR}_{\text{Low}}$, Target = $\text{Entry} + 2.0 \times R_{\text{trade}}$.
- **Short Entry**: Bar closes $< \text{OR}_{\text{Low}}$ and $< \text{VWAP}$. Initial SL = $\text{OR}_{\text{High}}$, Target = $\text{Entry} - 2.0 \times R_{\text{trade}}$.
- **Breakeven Trailing**: When favorable excursion reaches $+1.0 \times R_{\text{trade}}$, Stop Loss moves to breakeven.

---

### 2. Central Pivot Range (CPR) Regime Breakout & Mean-Reversion (`cpr`)
Floor-pivot framework derived from the prior session's High, Low, and Close, coupled with a dynamic volatility-regime filter:
- **Pivots**:
  $$P = \frac{\text{High} + \text{Low} + \text{Close}}{3}, \quad BC = \frac{\text{High} + \text{Low}}{2}, \quad TC = (P - BC) + P = 2P - BC$$
  $$\text{CPR Width \%} = \frac{|TC - BC|}{P} \times 100$$
- **Regime Classification (Trailing 20-Day Percentile)**:
  - **Narrow CPR ($\le 20\text{th}$ percentile)**: Trend / Expansion Day. Long breakout on close $> TC$; Short breakdown on close $< BC$. Target = $P \pm \text{Prior Range}$, Stop = $P$.
  - **Wide CPR ($\ge 80\text{th}$ percentile)**: Range / Mean-Reversion Day. Fade long at $BC$ holding support; fade short at $TC$ rejecting resistance.
  - **Neutral CPR ($20\text{th} - 80\text{th}$ percentile)**: Sits out to protect capital from low-edge chop.

---

### 3. Adaptive Volatility-Buffered Dual-EMA Trend System (`dual_ema`)
Continuous trend-following model using EMA9/EMA21 crossovers with an ATR-scaled no-trade buffer to reject whipsaws and an SMA200 higher-timeframe trend filter:
- **Indicators**: Running EMA9, EMA21, ATR14, and SMA200 seeded via cross-day history.
- **Buffer**: $\text{Buffer} = 0.175 \times \text{ATR}_{14}$.
- **Long Entry**: $\text{Close} > \text{EMA}_9 > (\text{EMA}_{21} + \text{Buffer})$ AND $\text{Close} > \text{SMA}_{200}$.
  - Stop Loss: $\text{Close} - (1.2 \times \text{ATR}_{14})$, Target: $\text{Close} + (2.0 \times \text{ATR}_{14})$.
- **Short Entry**: $\text{Close} < \text{EMA}_9 < (\text{EMA}_{21} - \text{Buffer})$ AND $\text{Close} < \text{SMA}_{200}$.
  - Stop Loss: $\text{Close} + (1.2 \times \text{ATR}_{14})$, Target: $\text{Close} - (2.0 \times \text{ATR}_{14})$.

---

## 🔍 NIFTY 50 Universe Scanner (`scanner/stock_ranker.py`)

A transparent, quantitative 100-point scoring algorithm (no black-box predictions) that scans candidate equities:
- **RVOL (30 pts)**: Institutional volume participation vs 20-day baseline.
- **Gap % (25 pts)**: Overnight momentum expansion from previous close.
- **ATR % (25 pts)**: Volatility capacity ($\text{ATR}_{14} / \text{Close}$).
- **VWAP Clearance (20 pts)**: Directional expansion distance away from session VWAP.

Supports live batched quotes from Zerodha Kite Connect or realistic market state fallbacks.

---

## 🛡️ Risk Management & Portfolio Circuit-Breakers

- **True Economic Stop Sizing**: Sizing is computed strictly using economic distance from execution price to stop loss:
  $$\text{Quantity} = \left\lfloor \frac{\text{Capital} \times 0.01}{R_{\text{trade}}} \right\rfloor$$
  Floor-rounded to contract lot size (25 for NIFTY futures) and bounded by exchange margins.
- **Daily Kill-Switch (2.0% Capital Drawdown)**:
  1. Instantly closes all open positions across instruments.
  2. Cancels resting limit/stop orders across broker gateways.
  3. Halts new entries until the next trading day.
- **Session Time Invariants**:
  - Entry Window: **09:45 to 13:30 IST**.
  - Mandatory Square-Off: **14:30 IST** (eliminates late-session liquidity traps and broker square-off penalties).
  - Hard Cutoff: **15:10 IST**.
  - Max 1 trade per instrument per day; no overnight holds.

---

## 🏛️ Statutory SEBI Transaction Cost Schedule (Oct 2024 Revised)

All backtesting, walk-forward simulations, and live journals account for full statutory frictions:

| Cost Component | NIFTY Index Futures (MIS) | Equity Intraday (MIS) |
|---|---|---|
| **Brokerage** | Flat ₹20 per executed order | 0.03% or ₹20 (whichever lower) |
| **STT** | 0.02% on sell turnover (Revised Oct 2024) | 0.025% on sell turnover |
| **Exchange Charges** | 0.00190% on aggregate turnover | 0.00325% on aggregate turnover |
| **GST** | 18% on (Brokerage + Txn Charges + SEBI) | 18% on (Brokerage + Txn Charges + SEBI) |
| **SEBI Charges** | ₹10 per crore (0.0001%) | ₹10 per crore (0.0001%) |
| **Stamp Duty** | 0.002% on buy turnover | 0.003% on buy turnover |
| **Slippage Deduction** | 0.50 index points per round-trip | 0.02% or 1 tick |

---

## 📂 Architecture Overview

```
trading algorithm/
├── config/
│   ├── settings.py              # Pydantic BaseSettings, risk rules, schedule
│   └── universe.py              # NIFTY 50 constituent registry & Kite token resolver
├── data/
│   ├── candle_aggregator.py     # Real-time tick to 15m candle & VWAP aggregator
│   ├── historical_loader.py     # Validated cache loader & Kite Historical API fetcher
│   └── market_calendar.py       # NSE session phases & holiday calendar
├── scanner/
│   └── stock_ranker.py          # Explainable 100-pt NIFTY 50 universe ranker
├── strategy/
│   ├── base_strategy.py         # Abstract BaseStrategy & StrategySignal models
│   ├── orb_strategy.py          # 30-Minute ORB + VWAP Strategy
│   ├── cpr_strategy.py          # Central Pivot Range Regime Breakout Strategy
│   └── dual_ema_strategy.py     # Volatility-Buffered Dual-EMA Strategy
├── backtest/
│   ├── strategy_backtester.py   # Generic strategy-agnostic backtester
│   ├── event_engine.py          # ORB+VWAP Event-driven backtest engine
│   ├── rolling_walk_forward.py  # Sequential expanding-window walk-forward engine
│   ├── walk_forward.py          # Static 70/30 in-sample/out-of-sample validator
│   └── performance.py           # Institutional analytics (Sharpe, CAGR, Expectancy)
├── risk/
│   ├── position_sizer.py        # True economic risk fractional position sizing
│   ├── risk_manager.py          # Pre-trade gates & 2% daily loss kill-switch
│   └── transaction_costs.py     # Exact Oct 2024 SEBI statutory friction model
├── broker/
│   ├── base_broker.py           # Abstract broker gateway
│   ├── paper_broker.py          # Simulated broker engine with order book tracking
│   ├── kite_adapter.py          # Zerodha Kite Connect v3 adapter
│   └── dhan_adapter.py          # DhanHQ API v2 adapter
├── execution/
│   ├── order_manager.py         # Order state machine & idempotency deduplication
│   └── execution_engine.py      # Real-time orchestrator & startup reconciliation
├── portfolio/
│   └── portfolio_manager.py     # Portfolio capital, daily P&L, and position state
├── database/
│   ├── db.py                    # SQLite database manager
│   └── models.py                # TradeRecord and OrderRecord schemas
├── backend/                     # FastAPI REST API
├── frontend/                    # React + Vite dark mode dashboard
├── tests/                       # 90-test comprehensive automated test suite
├── run_algo.py                  # CLI runner (backtest, rolling, scan, paper, live)
└── package.json
```

---

## ⚡ Quickstart & Usage Commands

### 1. Run Strategy-Specific Backtest
```bash
source .venv/bin/activate

# 1. CPR Regime Breakout Strategy
python run_algo.py --mode backtest --strategy cpr

# 2. Volatility-Buffered Dual-EMA Strategy
python run_algo.py --mode backtest --strategy dual_ema

# 3. 30-Minute ORB Strategy (Default)
python run_algo.py --mode backtest --strategy orb
```
Outputs institutional performance metrics: Net P&L, Win Rate, Profit Factor, Sharpe Ratio, CAGR, Max Drawdown, Expectancy, and Total Frictions.

### 2. Run Rolling Walk-Forward Simulation
```bash
# Day-by-day anchored expanding walk-forward across unseen 20-day blocks
python run_algo.py --mode rolling --strategy cpr
python run_algo.py --mode rolling --strategy orb
```
Outputs fold-by-fold results, combined out-of-sample equity curve, and fold aggregate robustness statistics (Median Profit Factor, Median Sharpe, % Profitable Folds, P&L Dispersion).

### 3. Run NIFTY 50 Universe Scanner
```bash
# Display top 5 momentum & breakout candidates with transparent scoring breakdown
python run_algo.py --mode scan --top-n 5

# Scan candidates and immediately backtest the top picks
python run_algo.py --mode backtest --scan --top-n 3
```

### 4. Run Paper Trading Simulation
```bash
python run_algo.py --mode paper
```
Runs a simulated intraday session with real-time ASCII telemetry, risk gate enforcement, and SQLite trade recording.

### 5. Launch Web Application
```bash
npm run dev
```
Access the dark-mode dashboard at **[http://localhost:5173](http://localhost:5173)** with Kite Connect authentication, margins overview, live telemetry, and trade journal.

### 6. Run Automated Test Suite
```bash
pytest -v
```
Executes the full test suite (**90 tests passing**), verifying no-lookahead assertions, broker outage recovery, signal idempotency, same-candle conservative execution, SEBI cost calculations, and strategy execution.
