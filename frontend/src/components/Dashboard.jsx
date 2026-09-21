import React, { useState, useEffect, useCallback } from 'react';
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

  const [telemetry, setTelemetry] = useState(null);
  const [scannerData, setScannerData] = useState(null);
  const [margins, setMargins] = useState(null);
  const [positionsData, setPositionsData] = useState({ positions: [], count: 0, total_unrealised_pnl: 0, total_realised_pnl: 0, total_pnl: 0 });
  const [trades, setTrades] = useState([]);
  const [orders, setOrders] = useState([]);
  const [health, setHealth] = useState(null);
  const [backtestReport, setBacktestReport] = useState(null);
  const [loadingBacktest, setLoadingBacktest] = useState(false);
  const [loadingScanner, setLoadingScanner] = useState(false);
  const [tradingMode, setTradingMode] = useState('PAPER'); // 'PAPER' | 'LIVE'
  const [showSettings, setShowSettings] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);

  // Fetch telemetry, positions, and market updates
  const fetchData = useCallback(async () => {
    try {
      const [telRes, marRes, posRes, trdRes, ordRes, hltRes] = await Promise.all([
        fetch(`/api/strategy/telemetry?symbol=${encodeURIComponent(selectedSymbol)}&strategy=${encodeURIComponent(selectedStrategy)}`),
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
      console.warn('Dashboard fetch error:', e);
    }
  }, [selectedSymbol, selectedStrategy]);


  // Fetch universe scan results
  const fetchScanner = useCallback(async () => {
    setLoadingScanner(true);
    try {
      const res = await fetch('/api/strategy/scanner?top_n=50');
      if (res.ok) {
        const data = await res.json();
        setScannerData(data);
      }
    } catch (e) {
      console.warn('Scanner fetch error:', e);
    } finally {
      setLoadingScanner(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    fetchScanner();
    const interval = setInterval(fetchData, 3000);
    return () => clearInterval(interval);
  }, [fetchData, fetchScanner]);

  const handleRunBacktest = async (strat, sym) => {
    setLoadingBacktest(true);
    const targetStrat = strat || selectedStrategy;
    const targetSym = sym || selectedSymbol;
    try {
      const res = await fetch(
        `/api/strategy/backtest?days=180&symbol=${encodeURIComponent(targetSym)}&strategy=${encodeURIComponent(targetStrat)}`,
        { method: 'POST' }
      );
      if (res.ok) {
        const data = await res.json();
        setBacktestReport(data.report);
      } else {
        const err = await res.json();
        alert(`Backtest Notice: ${err.detail || 'Could not execute backtest on real data.'}`);
      }
    } catch (e) {
      alert(`Backtest error: ${e.message}`);
    } finally {
      setLoadingBacktest(false);
    }
  };

  const handleQuickExecute = () => {
    fetchData();
  };

  const handleSquareOff = async () => {
    if (window.confirm(`Are you sure you want to square off open position on ${selectedSymbol}?`)) {
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
          alert(data.message || 'Position squared off successfully.');
          fetchData();
        } else {
          alert(`Square-off failed: ${data.detail || 'Unknown error'}`);
        }
      } catch (e) {
        alert('Error squaring off: ' + e.message);
      }
    }
  };

  return (
    <div className="min-h-screen flex bg-[#090d16] text-slate-100 antialiased">
      {/* 1. Left Sidebar Navigation */}
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
        {/* 2. Top Navbar */}
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
        <main className="flex-1 p-4 sm:p-6 space-y-4 max-w-[1600px] w-full mx-auto">
          {activeTab === 'dashboard' && (
            <div className="space-y-4 animate-fade-in">
              {/* Top KPI Cards Row */}
              <TopKpiBar telemetry={telemetry} margins={margins} />

              {/* Main Real Dashboard Grid (~60% Chart / ~40% Strategy & Signal) */}
              <div className="grid grid-cols-1 lg:grid-cols-12 gap-4 items-start">
                {/* Left Column (~58% - 7 Cols): Candlestick Chart, Timeline, Market Levels */}
                <div className="lg:col-span-7 space-y-4">
                  <MarketStrategyPanel telemetry={telemetry} />
                  <MarketSessionTimeline
                    currentPhase={telemetry?.market_phase || 'ENTRY_SCANNING'}
                  />
                  <MarketLevelsCard telemetry={telemetry} />
                </div>

                {/* Right Column (~42% - 5 Cols): Strategy Status, Signal, Active Trade, Order Panel, Risk */}
                <div className="lg:col-span-5 space-y-4">
                  <StrategyStatusCard telemetry={telemetry} />
                  <SignalCard
                    signal={telemetry?.active_signal}
                    onQuickExecute={handleQuickExecute}
                    isLiveMode={tradingMode === 'LIVE'}
                  />
                  <ActiveTradeCard
                    trade={telemetry?.active_trade}
                    onSquareOff={handleSquareOff}
                  />
                  <OrderExecutionPanel
                    tradingMode={tradingMode}
                    setTradingMode={setTradingMode}
                    onOrderPlaced={fetchData}
                    ltp={telemetry?.current_price}
                    symbol={selectedSymbol}
                  />
                  <RiskManagementPanel telemetry={telemetry} />
                </div>
              </div>

              {/* Subsystem Health Bar at bottom */}
              <SystemHealthBar health={health} />
            </div>
          )}

          {activeTab === 'scanner' && (
            <div className="animate-fade-in">
              <ScannerPage
                scannerData={scannerData}
                isLoading={loadingScanner}
                onRefresh={fetchScanner}
                onSelectStock={(sym) => {
                  setSelectedSymbol(sym);
                  setActiveTab('dashboard');
                }}
              />
            </div>
          )}

          {activeTab === 'strategy' && (
            <div className="animate-fade-in">
              <StrategyPage
                telemetry={telemetry}
                selectedStrategy={selectedStrategy}
                onSelectStrategy={setSelectedStrategy}
              />
            </div>
          )}

          {activeTab === 'positions' && (
            <div className="animate-fade-in">
              <PositionsPage
                positionsData={positionsData}
                trades={trades}
                tradingMode={tradingMode}
                onRefresh={fetchData}
              />
            </div>
          )}


          {activeTab === 'orders' && (
            <div className="animate-fade-in">
              <OrdersPage orders={orders} onRefresh={fetchData} />
            </div>
          )}

          {activeTab === 'risk' && (
            <div className="animate-fade-in">
              <RiskPage telemetry={telemetry} />
            </div>
          )}

          {activeTab === 'performance' && (
            <div className="animate-fade-in">
              <PerformancePage
                backtestReport={backtestReport}
                onRunBacktest={handleRunBacktest}
                isLoading={loadingBacktest}
                selectedStrategy={selectedStrategy}
                selectedSymbol={selectedSymbol}
                onSelectStrategy={setSelectedStrategy}
              />
            </div>
          )}

          {activeTab === 'system' && (
            <div className="animate-fade-in">
              <SystemPage health={health} />
            </div>
          )}
        </main>
      </div>

      {/* Settings Modal */}
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
