import React, { useState, useEffect } from 'react';
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
import PositionsPage from './terminal/PositionsPage';
import OrdersPage from './terminal/OrdersPage';
import PerformancePage from './terminal/PerformancePage';
import StrategyPage from './terminal/StrategyPage';
import RiskPage from './terminal/RiskPage';
import SystemPage from './terminal/SystemPage';

export default function Dashboard({ user, onLogout }) {
  const [activeTab, setActiveTab] = useState('dashboard');
  const [telemetry, setTelemetry] = useState(null);
  const [margins, setMargins] = useState(null);
  const [trades, setTrades] = useState([]);
  const [orders, setOrders] = useState([]);
  const [health, setHealth] = useState(null);
  const [backtestReport, setBacktestReport] = useState(null);
  const [loadingBacktest, setLoadingBacktest] = useState(false);
  const [tradingMode, setTradingMode] = useState('PAPER'); // 'PAPER' | 'LIVE'
  const [showSettings, setShowSettings] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);

  // Polling data at a clean interval
  const fetchData = async () => {
    try {
      const [telRes, marRes, trdRes, ordRes, hltRes] = await Promise.all([
        fetch('/api/strategy/telemetry'),
        fetch('/api/margins'),
        fetch('/api/strategy/trades'),
        fetch('/api/strategy/orders'),
        fetch('/api/system/health'),
      ]);

      if (telRes.ok) setTelemetry(await telRes.json());
      if (marRes.ok) setMargins(await marRes.json());
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
      console.warn('Dashboard fetch telemetry warning:', e);
    }
  };

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 3000);
    return () => clearInterval(interval);
  }, []);

  const handleRunBacktest = async () => {
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

  const handleQuickExecute = (sig) => {
    fetchData();
  };

  const handleSquareOff = async () => {
    if (window.confirm('Are you sure you want to square off your open position immediately?')) {
      try {
        await fetch('/api/orders/place', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            symbol: 'NIFTY',
            direction: telemetry?.active_trade?.direction === 'BUY' ? 'SELL' : 'BUY',
            order_type: 'MARKET',
            quantity: telemetry?.active_trade?.quantity || 25,
            mode: tradingMode,
          }),
        });
        fetchData();
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
                  />
                  <RiskManagementPanel telemetry={telemetry} />
                </div>
              </div>

              {/* Subsystem Health Bar at bottom */}
              <SystemHealthBar health={health} />
            </div>
          )}

          {activeTab === 'strategy' && (
            <div className="animate-fade-in">
              <StrategyPage telemetry={telemetry} />
            </div>
          )}

          {activeTab === 'positions' && (
            <div className="animate-fade-in">
              <PositionsPage
                trades={trades}
                activeTrade={telemetry?.active_trade}
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
