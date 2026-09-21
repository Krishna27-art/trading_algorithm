import React, { useState, useEffect, useCallback } from 'react';
import {
  LayoutDashboard,
  Compass,
  Layers,
  FileText,
  ShieldCheck,
  BarChart3,
  Server,
  Settings,
  RefreshCw,
  Zap,
  TrendingUp,
  TrendingDown,
  Activity,
  CheckCircle2,
  AlertTriangle,
} from 'lucide-react';

import Sidebar from './terminal/Sidebar';
import TopNavbar from './terminal/TopNavbar';
import TopKpiBar from './terminal/TopKpiBar';
import MarketStrategyPanel from './terminal/MarketStrategyPanel';
import MarketLevelsCard from './terminal/MarketLevelsCard';
import StrategyStatusCard from './terminal/StrategyStatusCard';
import SignalCard from './terminal/SignalCard';
import ActiveTradeCard from './terminal/ActiveTradeCard';
import OrderExecutionPanel from './terminal/OrderExecutionPanel';
import RiskManagementPanel from './terminal/RiskManagementPanel';
import MarketSessionTimeline from './terminal/MarketSessionTimeline';
import SystemHealthBar from './terminal/SystemHealthBar';
import SettingsModal from './terminal/SettingsModal';

// Research First Components
import ResearchPredictionTable from './terminal/ResearchPredictionTable';
import KeyInsightsPanel from './terminal/KeyInsightsPanel';
import MultiStrategyBacktestPanel from './terminal/MultiStrategyBacktestPanel';
import QuickOrderModal from './terminal/QuickOrderModal';

// Dedicated Subpages
import ScannerPage from './terminal/ScannerPage';
import PositionsPage from './terminal/PositionsPage';
import OrdersPage from './terminal/OrdersPage';
import PerformancePage from './terminal/PerformancePage';
import StrategyPage from './terminal/StrategyPage';
import RiskPage from './terminal/RiskPage';
import SystemPage from './terminal/SystemPage';

export default function Dashboard({ user, onLogout }) {
  const [activeTab, setActiveTab] = useState('dashboard');
  const [selectedStrategy, setSelectedStrategy] = useState('cpr'); // 'cpr' | 'dual_ema' | 'orb'
  const [selectedSymbol, setSelectedSymbol] = useState('NIFTY');

  // Unified Research State
  const [researchData, setResearchData] = useState(null);
  const [loadingResearch, setLoadingResearch] = useState(false);
  const [researchBacktestData, setResearchBacktestData] = useState(null);
  const [loadingBacktest, setLoadingBacktest] = useState(false);
  const [lastUpdatedTime, setLastUpdatedTime] = useState('');

  // Terminal telemetry and portfolio states
  const [telemetry, setTelemetry] = useState(null);
  const [margins, setMargins] = useState(null);
  const [positionsData, setPositionsData] = useState({
    positions: [],
    count: 0,
    total_unrealised_pnl: 0,
    total_realised_pnl: 0,
    total_pnl: 0,
  });
  const [trades, setTrades] = useState([]);
  const [orders, setOrders] = useState([]);
  const [health, setHealth] = useState(null);

  // Trading mode and execution modal
  const [tradingMode, setTradingMode] = useState('PAPER'); // 'PAPER' | 'LIVE'
  const [showSettings, setShowSettings] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [orderModalOpen, setOrderModalOpen] = useState(false);
  const [orderModalData, setOrderModalData] = useState(null);

  // Update IST clock string
  const formatIST = () => {
    const now = new Date();
    return new Intl.DateTimeFormat('en-GB', {
      timeZone: 'Asia/Kolkata',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    }).format(now);
  };

  // 1. Fetch live multi-strategy research predictions
  const fetchResearchLive = useCallback(async (force = false) => {
    setLoadingResearch(true);
    try {
      const url = `/api/research/live?top_n=10${force ? '&force_refresh=true' : ''}`;
      const res = await fetch(url);
      if (res.ok) {
        const data = await res.json();
        setResearchData(data);
        setLastUpdatedTime(formatIST());
      }
    } catch (e) {
      console.warn('Research live fetch failed:', e);
    } finally {
      setLoadingResearch(false);
    }
  }, []);

  // 2. Fetch 180-day 3-strategy backtest report
  const fetchResearchBacktest = useCallback(async (days = 180, symbol = selectedSymbol) => {
    setLoadingBacktest(true);
    try {
      const url = `/api/research/backtest?days=${days}&symbol=${encodeURIComponent(symbol)}`;
      const res = await fetch(url);
      if (res.ok) {
        const data = await res.json();
        setResearchBacktestData(data);
      }
    } catch (e) {
      console.warn('Research backtest fetch failed:', e);
    } finally {
      setLoadingBacktest(false);
    }
  }, [selectedSymbol]);

  // 3. Fetch telemetry, positions, and orders
  const fetchTerminalData = useCallback(async () => {
    try {
      const [telRes, marRes, posRes, trdRes, ordRes, hltRes] = await Promise.all([
        fetch(
          `/api/strategy/telemetry?symbol=${encodeURIComponent(
            selectedSymbol
          )}&strategy=${encodeURIComponent(selectedStrategy)}`
        ),
        fetch('/api/margins'),
        fetch('/api/portfolio/positions'),
        fetch('/api/strategy/trades'),
        fetch('/api/strategy/orders'),
        fetch('/api/system/health'),
      ]);

      if (telRes.ok) setTelemetry(await telRes.json());
      if (marRes.ok) setMargins(await marRes.json());
      if (posRes.ok) setPositionsData(await posRes.json());
      if (trdRes.ok) {
        const d = await trdRes.json();
        setTrades(d.trades || []);
      }
      if (ordRes.ok) {
        const d = await ordRes.json();
        setOrders(d.orders || []);
      }
      if (hltRes.ok) setHealth(await hltRes.json());
    } catch (e) {
      console.warn('Terminal data fetch error:', e);
    }
  }, [selectedSymbol, selectedStrategy]);

  // Unified refresh
  const refreshAll = useCallback(() => {
    fetchResearchLive(true);
    fetchTerminalData();
  }, [fetchResearchLive, fetchTerminalData]);

  // Initial mount and interval polling
  useEffect(() => {
    fetchResearchLive();
    fetchResearchBacktest(180, 'NIFTY');
    fetchTerminalData();

    // Poll live quotes & predictions every 5s
    const pollTimer = setInterval(() => {
      fetchResearchLive(false);
      fetchTerminalData();
    }, 5000);

    return () => clearInterval(pollTimer);
  }, [fetchResearchLive, fetchResearchBacktest, fetchTerminalData]);

  // Quick Order Execution Handlers
  const handleTradeCandidate = (cand) => {
    const orb = cand.predictions?.orb;
    const cpr = cand.predictions?.cpr;
    const dual = cand.predictions?.dual_ema;

    // Pick signal from agreeing strategy
    const activeSig =
      (orb && orb.direction ? orb : null) ||
      (cpr && cpr.direction ? cpr : null) ||
      (dual && dual.direction ? dual : null);

    setOrderModalData({
      symbol: cand.symbol,
      ltp: cand.ltp,
      direction: activeSig?.direction || (cand.universe_bias === 'SHORT' ? 'SELL' : 'BUY'),
      entry: activeSig?.entry || cand.ltp,
      stop_loss: activeSig?.stop_loss || null,
      target: activeSig?.target || null,
      quantity: cand.symbol === 'NIFTY' ? 65 : 1,
    });
    setOrderModalOpen(true);
  };

  const handleQuickExecuteSignal = (signal) => {
    if (!signal) return;
    setOrderModalData({
      symbol: signal.symbol || selectedSymbol,
      direction: signal.type === 'SELL' ? 'SELL' : 'BUY',
      entry: signal.entry,
      stop_loss: signal.stop_loss,
      target: signal.target,
      quantity: signal.quantity || (selectedSymbol === 'NIFTY' ? 65 : 1),
    });
    setOrderModalOpen(true);
  };

  const handleSquareOff = async () => {
    if (window.confirm(`Confirm immediate square-off for ${selectedSymbol}?`)) {
      try {
        const res = await fetch('/api/orders/exit', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Shared-Secret': localStorage.getItem('app_shared_secret') || '',
          },
          body: JSON.stringify({
            symbol: selectedSymbol,
            mode: tradingMode,
          }),
        });
        const data = await res.json();
        if (data.success) {
          alert(data.message || 'Position squared off.');
          fetchTerminalData();
        } else {
          alert(`Square-off failed: ${data.detail || 'Error'}`);
        }
      } catch (e) {
        alert('Error: ' + e.message);
      }
    }
  };

  const isKiteConnected = Boolean(telemetry?.authenticated || health?.kite_api === 'CONNECTED');
  const isMarketOpen = (telemetry?.market_status || researchData?.market_status) === 'OPEN';

  return (
    <div className="min-h-screen flex bg-[#090d16] text-slate-100 antialiased font-sans">
      {/* 1. Left Navigation Sidebar */}
      <Sidebar
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        user={user}
        onLogout={onLogout}
        onOpenSettings={() => setShowSettings(true)}
        telemetry={telemetry}
        tradingMode={tradingMode}
        setTradingMode={setTradingMode}
      />

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* 2. Top Navigation Bar */}
        <TopNavbar
          user={user}
          telemetry={telemetry}
          selectedStrategy={selectedStrategy}
          onSelectStrategy={setSelectedStrategy}
          selectedSymbol={selectedSymbol}
          onSelectSymbol={setSelectedSymbol}
          onLogout={onLogout}
          onOpenSettings={() => setShowSettings(true)}
          tradingMode={tradingMode}
          setTradingMode={setTradingMode}
          onToggleMobileSidebar={() => setMobileSidebarOpen(!mobileSidebarOpen)}
        />

        {/* Dynamic Page Container */}
        <main className="flex-1 p-4 sm:p-6 space-y-6 max-w-[1680px] w-full mx-auto">
          {/* TAB 1: PREDICTIONS HUB (MAIN RESEARCH DASHBOARD) */}
          {activeTab === 'dashboard' && (
            <div className="space-y-6 animate-fade-in">
              {/* Research Header Banner */}
              <div className="p-5 rounded-2xl bg-gradient-to-r from-[#0e1526] via-[#101930] to-[#0e1526] border border-white/10 shadow-xl flex flex-wrap items-center justify-between gap-4">
                <div className="space-y-1">
                  <div className="flex items-center gap-2.5">
                    <div className="w-8 h-8 rounded-lg bg-indigo-600 flex items-center justify-center font-mono font-bold text-white text-sm shadow-md">
                      TR
                    </div>
                    <h1 className="text-lg sm:text-xl font-black text-white tracking-tight font-mono">
                      Trading Algorithm & Market Consensus
                    </h1>
                  </div>
                  <p className="text-xs text-slate-400">
                    Real-time cross-strategy validation across 50 NIFTY index constituents (ORB + CPR + Dual-EMA)
                  </p>
                </div>

                {/* State Indicators & Actions */}
                <div className="flex flex-wrap items-center gap-2.5 font-mono text-xs">
                  {/* Kite Status */}
                  <div
                    className={`px-3 py-1 rounded-lg border flex items-center gap-1.5 font-bold ${
                      isKiteConnected
                        ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                        : 'bg-rose-500/10 text-rose-400 border-rose-500/30'
                    }`}
                  >
                    <span className={`w-2 h-2 rounded-full ${isKiteConnected ? 'bg-emerald-400' : 'bg-rose-500'}`} />
                    <span>{isKiteConnected ? 'REAL KITE' : 'OFFLINE'}</span>
                  </div>

                  {/* Market Status */}
                  <div className="px-3 py-1 rounded-lg bg-white/[0.03] border border-white/5 flex items-center gap-1.5 font-bold text-slate-200">
                    <span className={`w-2 h-2 rounded-full ${isMarketOpen ? 'bg-emerald-400' : 'bg-rose-400'}`} />
                    <span>{isMarketOpen ? 'MARKET OPEN' : 'MARKET CLOSED'}</span>
                  </div>

                  {/* Mode Badge */}
                  <div
                    className={`px-3 py-1 rounded-lg border font-bold ${
                      tradingMode === 'LIVE'
                        ? 'bg-rose-500/15 text-rose-300 border-rose-500/40'
                        : 'bg-indigo-500/15 text-indigo-300 border-indigo-500/30'
                    }`}
                  >
                    {tradingMode === 'LIVE' ? 'LIVE MONEY' : 'PAPER SIMULATION'}
                  </div>

                  {/* Timestamp */}
                  <div className="px-3 py-1 rounded-lg bg-black/40 border border-white/5 text-slate-400">
                    Last Updated: {lastUpdatedTime || formatIST()} IST
                  </div>

                  {/* Manual Refresh Button */}
                  <button
                    onClick={refreshAll}
                    disabled={loadingResearch}
                    className="p-1.5 rounded-lg bg-white/5 hover:bg-white/10 text-slate-300 hover:text-white border border-white/10 transition-colors"
                    title="Force refresh scan"
                  >
                    <RefreshCw className={`w-4 h-4 ${loadingResearch ? 'animate-spin' : ''}`} />
                  </button>
                </div>
              </div>

              {/* SECTION 1: MULTI-STRATEGY UNIVERSE SCAN & LIVE PREDICTIONS */}
              <ResearchPredictionTable
                candidates={researchData?.candidates || []}
                isLoading={loadingResearch}
                dataSource={researchData?.data_source || 'NONE'}
                onSelectCandidate={(sym) => {
                  setSelectedSymbol(sym);
                }}
                onTradeCandidate={handleTradeCandidate}
              />

              {/* SECTION 2: KEY INSIGHTS SECTION */}
              <KeyInsightsPanel
                keyInsights={researchData?.key_insights}
                onSelectCandidate={(sym) => {
                  setSelectedSymbol(sym);
                }}
              />

              {/* SECTION 3: 180-DAY BACKTEST ALL THREE STRATEGIES */}
              <MultiStrategyBacktestPanel
                backtestData={researchBacktestData}
                isLoading={loadingBacktest}
                onRunBacktest={(d, sym) => fetchResearchBacktest(d, sym)}
                selectedSymbol={selectedSymbol}
              />
            </div>
          )}

          {/* TAB 2: TERMINAL WORKSTATION (CHARTS, TIMELINE, ACTIVE TRADE & EXECUTION) */}
          {activeTab === 'terminal' && (
            <div className="space-y-4 animate-fade-in">
              <TopKpiBar telemetry={telemetry} margins={margins} />

              <div className="grid grid-cols-1 lg:grid-cols-12 gap-4 items-start">
                {/* Left Column (Chart, Timeline, Levels) */}
                <div className="lg:col-span-7 space-y-4">
                  <MarketStrategyPanel telemetry={telemetry} />
                  <MarketSessionTimeline
                    currentPhase={telemetry?.market_phase || 'ENTRY_SCANNING'}
                  />
                  <MarketLevelsCard telemetry={telemetry} />
                </div>

                {/* Right Column (Status, Signal, Active Trade, Order Panel, Risk) */}
                <div className="lg:col-span-5 space-y-4">
                  <StrategyStatusCard telemetry={telemetry} />
                  <SignalCard
                    signal={telemetry?.active_signal}
                    onQuickExecute={handleQuickExecuteSignal}
                    isLiveMode={tradingMode === 'LIVE'}
                  />
                  <ActiveTradeCard
                    trade={telemetry?.active_trade}
                    onSquareOff={handleSquareOff}
                  />
                  <OrderExecutionPanel
                    tradingMode={tradingMode}
                    setTradingMode={setTradingMode}
                    onOrderPlaced={fetchTerminalData}
                    ltp={telemetry?.current_price}
                    symbol={selectedSymbol}
                  />
                  <RiskManagementPanel telemetry={telemetry} />
                </div>
              </div>

              <SystemHealthBar health={health} />
            </div>
          )}

          {/* TAB 3: POSITIONS PAGE */}
          {activeTab === 'positions' && (
            <div className="animate-fade-in">
              <PositionsPage
                positionsData={positionsData}
                trades={trades}
                tradingMode={tradingMode}
                onRefresh={fetchTerminalData}
              />
            </div>
          )}

          {/* TAB 4: ORDERS PAGE */}
          {activeTab === 'orders' && (
            <div className="animate-fade-in">
              <OrdersPage orders={orders} onRefresh={fetchTerminalData} />
            </div>
          )}

          {/* TAB 5: BACKTEST ANALYTICS */}
          {activeTab === 'performance' && (
            <div className="animate-fade-in">
              <PerformancePage
                backtestReport={researchBacktestData?.strategies?.[selectedStrategy]}
                backtestError={null}
                onRunBacktest={(strat, sym) => fetchResearchBacktest(180, sym)}
                isLoading={loadingBacktest}
                selectedStrategy={selectedStrategy}
                selectedSymbol={selectedSymbol}
                onSelectStrategy={setSelectedStrategy}
              />
            </div>
          )}

          {/* TAB 6: STRATEGY SPECIFICATION */}
          {activeTab === 'strategy' && (
            <div className="animate-fade-in">
              <StrategyPage
                telemetry={telemetry}
                selectedStrategy={selectedStrategy}
                onSelectStrategy={setSelectedStrategy}
              />
            </div>
          )}

          {/* TAB 7: RISK PARAMETERS */}
          {activeTab === 'risk' && (
            <div className="animate-fade-in">
              <RiskPage telemetry={telemetry} />
            </div>
          )}

          {/* TAB 8: SYSTEM HEALTH */}
          {activeTab === 'system' && (
            <div className="animate-fade-in">
              <SystemPage health={health} />
            </div>
          )}
        </main>
      </div>

      {/* Quick Order Execution Modal */}
      <QuickOrderModal
        isOpen={orderModalOpen}
        onClose={() => setOrderModalOpen(false)}
        initialData={orderModalData}
        onOrderSuccess={() => {
          fetchTerminalData();
          fetchResearchLive(true);
        }}
        tradingMode={tradingMode}
        setTradingMode={setTradingMode}
      />

      {/* Terminal Settings Modal */}
      {showSettings && (
        <SettingsModal
          onClose={() => setShowSettings(false)}
          strategyState={telemetry}
          user={user}
        />
      )}
    </div>
  );
}
