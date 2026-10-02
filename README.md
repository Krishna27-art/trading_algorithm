# Institutional NSE Intraday Signal & Research Workstation
### Multi-Strategy Quantitative Engine, 300-Stock Universe Scanner & Real-Time Kite Stream

A high-performance quantitative research and intraday signal generation platform for the **National Stock Exchange of India (NSE)**, calibrated for **NIFTY Index** and liquid equities from the **NSE 300-stock universe**.

> **Note on Execution Model**: This application is **read-only with respect to broker order execution**. It ingests real Zerodha Kite market data, runs intraday strategy engines, and generates actionable signals and consensus. All order placement and trade execution are performed manually by the trader outside the application.

---

## 📐 Quantitative Strategies & Engine Architectures

The platform maintains a registry of 7 quantitative strategy models, evaluating multi-timeframe price action, volatility regimes, momentum, and Level-5 order book microstructure:

### 1. 30-Minute Volatility-Filtered Opening Range Breakout (`orb`)
Captures morning price discovery momentum while strictly filtering out low-volatility chop and anchoring directional entries to session volume-weighted fair value.
- **Opening Range (09:15 – 09:45 IST)**:
  $$\text{OR}_{\text{High}} = \max_{t \in [09:15, 09:45]} (\text{High}_t), \quad \text{OR}_{\text{Low}} = \min_{t \in [09:15, 09:45]} (\text{Low}_t)$$
- **Volatility Filter**: Requires minimum opening range width (e.g. $\ge 40.0$ index pts on NIFTY or percentage thresholds on equities).
- **Session VWAP Confirmation**: Long entries require bar close $> \text{OR}_{\text{High}}$ and $> \text{VWAP}$; Short entries require bar close $< \text{OR}_{\text{Low}}$ and $< \text{VWAP}$.

### 2. Central Pivot Range (CPR) Regime Breakout & Mean-Reversion (`cpr`)
Floor-pivot framework derived from prior session High, Low, and Close, coupled with a 20-day historical CPR width percentile regime filter:
- **Pivots**:
  $$P = \frac{H + L + C}{3}, \quad BC = \frac{H + L}{2}, \quad TC = 2P - BC$$
- **Narrow CPR ($\le 20\text{th}$ percentile)**: Trend expansion bias; trade directional breakouts of $TC$ / $BC$.
- **Wide CPR ($\ge 80\text{th}$ percentile)**: Mean-reversion bias; fade reversals at CPR boundaries.

### 3. Adaptive Volatility-Buffered Dual-EMA Trend System (`dual_ema`)
Intraday trend-following system utilizing EMA9/EMA21 crossovers with an ATR-scaled buffer ($\text{Buffer} = \gamma \times \text{ATR}_{14}$) to reject whipsaws and an SMA200 higher-timeframe trend filter.

### 4. APEX Adaptive Intraday Volatility Expansion Model (`apex`)
Multi-factor intraday model incorporating overnight gap analysis from the 09:15 opening bar, Parkinson volatility, microprice deviation, and multi-timeframe momentum.

### 5. Sector Impulse & Peer Flow Strategy (`sector_impulse`)
Cross-sectional momentum strategy tracking peer dispersion and sector index relative strength across NSE sector baskets.

### 6. SSF Level-5 Order Book Microstructure Model (`ssf_l5_srm`)
Order book imbalance model driven by streaming Level-5 bid/ask depth snapshots, spread dynamics, and order flow imbalance (OFI).

### 7. Analytic Ornstein-Uhlenbeck Optimal-Stopping System (`aou_oss`)
Continuous-time mean-reversion model operating on rolling volume-weighted anchor (RVWAP) spreads with exact numerical Bertram/Leung-Li optimal entry thresholds and analytical stopping barriers.

---

## 🔍 300-Stock Universe Scanner (`scanner/stock_ranker.py`)

A quantitative 100-point scoring algorithm evaluating candidates across Large, Mid, and Small cap NSE segments:
- **RVOL (30 pts)**: Institutional volume participation vs 20-day full-session baseline ($\text{RVOL} = \text{Day Volume} / \text{Avg 20D Volume}$).
- **Gap % (25 pts)**: Overnight opening momentum expansion from prior close.
- **ATR % (25 pts)**: Normalized volatility capacity ($\text{ATR}_{14} / \text{Close}$).
- **VWAP Clearance (20 pts)**: Directional expansion distance from session VWAP.

Fails closed on missing historical data or unauthenticated sessions without inventing fallback baselines.

---

## ⚡ Streaming Market Infrastructure (`streaming/`)

The platform features an event-driven streaming pipeline connected to Zerodha KiteTicker:
```
Zerodha KiteTicker (MODE_FULL)
             ↓ ticks
MarketStreamManager (connection state, heartbeat, token mapping)
             ↓
MultiSymbolCandleAggregator (15m OHLCV, VWAP, Level-5 Order Book Snapshots)
             ↓ candle completion / depth updates
LiveMarketState (in-memory state store) & LiveSignalEngine (real-time prediction evaluation)
             ↓
FastAPI Endpoints (/api/stream/signals, /api/stream/market)
             ↓
React Frontend Dashboard
```

---

## 🛡️ Risk Management & Portfolio Circuit-Breakers

- **True Economic Stop Sizing**: Sizing calculated strictly from economic risk distance to stop loss:
  $$\text{Quantity} = \left\lfloor \frac{\text{Capital} \times 0.01}{R_{\text{trade}}} \right\rfloor$$
  Floor-rounded to lot size and constrained by exchange margin requirements.
- **Daily Kill-Switch (2.0% Capital Drawdown)**: Halts signal generation and warns upon breaching daily maximum loss.
- **Strict Session Schedule (Asia/Kolkata)**:
  - Market Open: **09:15 IST**
  - Strategy Entry Window: **09:45 to 13:30 IST**
  - Mandatory Square-Off: **14:30 IST**
  - Session Cutoff: **15:10 IST**

---

## 🏛️ Statutory SEBI Transaction Cost Schedule (Oct 2024 Revised)

All backtests and performance evaluations model full statutory frictions:

| Cost Component | NIFTY Index Futures (MIS) | Equity Intraday (MIS) |
|---|---|---|
| **Brokerage** | Flat ₹20 per executed order | 0.03% or ₹20 (whichever lower) |
| **STT** | 0.02% on sell turnover (Oct 2024) | 0.025% on sell turnover |
| **Exchange Charges** | 0.00190% on aggregate turnover | 0.00325% on aggregate turnover |
| **GST** | 18% on (Brokerage + Txn Charges + SEBI) | 18% on (Brokerage + Txn Charges + SEBI) |
| **SEBI Charges** | ₹10 per crore (0.0001%) | ₹10 per crore (0.0001%) |
| **Stamp Duty** | 0.002% on buy turnover | 0.003% on buy turnover |
| **Slippage Deduction** | 0.50 index points per round-trip | 0.02% or 1 tick |

---

## 📂 Repository Architecture

```
trading algorithm/
├── backend/
│   ├── kite.py                  # Zerodha OAuth authentication & session routes
│   ├── market.py                # Lightweight live market quotes endpoint
│   ├── positions.py             # Broker positions & strategy trade matching
│   ├── signals.py               # Universe scanner, live research, backtest & telemetry routes
│   ├── stream_routes.py         # KiteTicker stream control & live signal endpoints
│   └── system.py                # System health monitoring
├── broker/
│   ├── kite_adapter.py          # Zerodha REST + KiteTicker broker adapter
│   └── paper_adapter.py         # Isolated simulation adapter
├── config/
│   ├── settings.py              # Pydantic configuration, instrument schemas, risk rules
│   └── universe.py              # 300-stock universe master & token resolver
├── data/
│   ├── candle_aggregator.py     # Real-time tick to 15m candle & VWAP aggregator
│   ├── historical_loader.py     # Validated cache loader & Kite Historical API fetcher
│   ├── instrument_resolver.py   # Master instrument resolver with 24h caching
│   ├── market_calendar.py       # NSE session phases & holiday calendar
│   ├── sector_peer_manager.py   # Sector classification & peer index manager
│   └── time_utils.py            # Centralized Asia/Kolkata (IST) timezone utilities
├── scanner/
│   ├── history_context_warmer.py# Background daily historical context pre-caching
│   ├── liquidity_filter.py      # Multi-tiered liquidity and tradability gates
│   └── stock_ranker.py          # 100-point explainable universe ranking engine
├── strategy/
│   ├── aou_oss_strategy.py      # Analytic OU Optimal-Stopping Strategy
│   ├── apex_engine.py           # APEX Adaptive Intraday Volatility Model
│   ├── base_strategy.py         # Abstract BaseStrategy & Signal dataclasses
│   ├── cpr_strategy.py          # CPR Regime Breakout Strategy
│   ├── dual_ema_strategy.py     # Buffered Dual-EMA Trend Strategy
│   ├── orb_strategy.py          # 30-Minute ORB + VWAP Strategy
│   ├── prediction_service.py    # Multi-strategy evaluation & live consensus engine
│   ├── sector_impulse_strategy.py # Sector Impulse Strategy
│   └── ssf_l5_srm_strategy.py   # SSF Level-5 Microstructure Strategy
├── streaming/
│   ├── live_market_state.py     # Thread-safe in-memory market quote & candle state
│   ├── live_signal_engine.py    # Event-driven streaming signal computation engine
│   └── market_stream_manager.py # KiteTicker WebSocket coordinator & state machine
├── backtest/
│   ├── performance.py           # Institutional analytics (Sharpe, CAGR, Expectancy)
│   ├── rolling_walk_forward.py  # Expanding-window walk-forward validation engine
│   └── strategy_backtester.py   # Strategy backtesting engine with journal isolation
├── database/
│   └── db.py                    # SQLite trade journal manager with live/backtest separation
├── frontend/                    # React + Vite dark mode trading dashboard
└── tests/                       # Comprehensive pytest suite (166+ tests)
```

---

## ⚡ Quickstart & Usage

### 1. Environment Configuration
Create a `.env` file with your Zerodha Kite Connect credentials:
```bash
KITE_API_KEY=your_kite_api_key
KITE_API_SECRET=your_kite_api_secret
FRONTEND_URL=http://localhost:5173
```

### 2. Strategy Backtesting
```bash
# CPR Regime Breakout
python run_algo.py --mode backtest --strategy cpr

# Volatility-Buffered Dual-EMA
python run_algo.py --mode backtest --strategy dual_ema

# 30-Minute ORB
python run_algo.py --mode backtest --strategy orb
```

### 3. Rolling Walk-Forward Analysis
```bash
python run_algo.py --mode rolling --strategy cpr
python run_algo.py --mode rolling --strategy orb
```

### 4. 300-Stock Universe Scanner
```bash
# Scan and rank top 5 momentum candidates
python run_algo.py --mode scan --top-n 5
```

### 5. Launch Full Workstation (Backend + Frontend)
```bash
# Start FastAPI backend (port 8000)
python backend/main.py

# Start React Frontend (port 5173)
npm run dev
```
Navigate to **[http://localhost:5173](http://localhost:5173)** to access the live dashboard, authenticate with Kite Connect, monitor live signals, and review trade logs.

### 6. Run Automated Test Suite
```bash
.venv/bin/pytest -v
```
Executes the comprehensive automated test suite (**166+ tests passing**), verifying data integrity, fail-closed data gating, consensus voting rules, risk gates, timezone accuracy, and streaming pipelines.
