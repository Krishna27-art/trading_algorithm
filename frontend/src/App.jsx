import { useEffect, useState } from 'react'
import { Loader2 } from 'lucide-react'
import ErrorBoundary from './components/common/ErrorBoundary'
import AppShell from './components/layout/AppShell'
import SettingsModal from './components/layout/SettingsModal'
import LoginPage from './pages/LoginPage'
import DashboardPage from './pages/DashboardPage'
import LiveSignalsPage from './pages/LiveSignalsPage'
import StocksPage from './pages/StocksPage'
import BacktestPage from './pages/BacktestPage'
import SystemStatusPage from './pages/SystemStatusPage'
import { getKiteStatus, kiteLogout } from './api/auth'

export default function App() {
  const [authState, setAuthState] = useState('checking') // checking | authenticated | guest | unauthenticated
  const [user, setUser] = useState(null)
  const [authError, setAuthError] = useState('')
  const [activeTab, setActiveTab] = useState('dashboard')
  const [showSettings, setShowSettings] = useState(false)

  useEffect(() => {
    let cancelled = false

    // Safety fallback: if backend is unreachable or slow, don't keep user on loading spinner
    const timeoutId = setTimeout(() => {
      if (!cancelled) {
        setAuthState('unauthenticated')
      }
    }, 3000)

    // Check for auth_error in query parameters if redirected from backend callback
    const urlParams = new URLSearchParams(window.location.search)
    const errParam = urlParams.get('auth_error')
    if (errParam) {
      setAuthError(errParam)
      window.history.replaceState({}, document.title, window.location.pathname)
    }

    getKiteStatus()
      .then((data) => {
        clearTimeout(timeoutId)
        if (cancelled) return
        if (data && data.connected) {
          setUser(data)
          setAuthState('authenticated')
        } else {
          setAuthState('unauthenticated')
        }
      })
      .catch(() => {
        clearTimeout(timeoutId)
        if (!cancelled) setAuthState('unauthenticated')
      })
    return () => {
      cancelled = true
      clearTimeout(timeoutId)
    }
  }, [])

  const handleContinueOffline = () => {
    setUser(null)
    setAuthState('guest')
  }

  const handleLogout = async () => {
    try {
      await kiteLogout()
    } catch {
      // Drop session locally
    }
    setUser(null)
    setAuthState('unauthenticated')
    setActiveTab('dashboard')
  }

  return (
    <ErrorBoundary>
      {authState === 'checking' ? (
        <div className="min-h-screen flex flex-col items-center justify-center gap-3 bg-[var(--bg)] text-[var(--text-dim)]">
          <Loader2 className="w-6 h-6 animate-spin text-[var(--accent)]" />
          <p className="text-sm">Checking Kite session status…</p>
        </div>
      ) : authState === 'unauthenticated' ? (
        <LoginPage onContinueOffline={handleContinueOffline} authError={authError} />
      ) : (
        <AppShell
          active={activeTab}
          onNavigate={setActiveTab}
          onOpenSettings={() => setShowSettings(true)}
          onLogout={handleLogout}
          user={user}
        >
          {activeTab === 'dashboard' && <DashboardPage onNavigate={setActiveTab} isAuthenticated={authState === 'authenticated'} />}
          {activeTab === 'signals' && <LiveSignalsPage isAuthenticated={authState === 'authenticated'} />}
          {activeTab === 'stocks' && <StocksPage isAuthenticated={authState === 'authenticated'} />}
          {activeTab === 'backtest' && <BacktestPage />}
          {activeTab === 'system' && <SystemStatusPage />}
        </AppShell>
      )}

      {showSettings && (
        <SettingsModal
          onClose={() => setShowSettings(false)}
        />
      )}
    </ErrorBoundary>
  )
}

