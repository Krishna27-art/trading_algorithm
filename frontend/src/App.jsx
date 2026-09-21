import React, { useState, useEffect, Component } from 'react';
import LoginPage from './components/LoginPage';
import Dashboard from './components/Dashboard';
import { Loader2, AlertTriangle, RefreshCw } from 'lucide-react';

class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null, errorInfo: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, errorInfo) {
    console.error('ErrorBoundary caught:', error, errorInfo);
    this.setState({ errorInfo });
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen bg-[#0a0e17] text-slate-200 flex flex-col items-center justify-center p-6">
          <div className="max-w-lg w-full bg-[#0f172a] border border-rose-500/30 rounded-xl p-6 shadow-2xl space-y-4">
            <div className="flex items-center gap-3 text-rose-400">
              <AlertTriangle className="w-8 h-8 shrink-0" />
              <div>
                <h2 className="text-lg font-bold text-white">Application Interface Error</h2>
                <p className="text-xs text-slate-400">A component encountered an unhandled exception.</p>
              </div>
            </div>

            <div className="bg-black/60 p-3 rounded-lg border border-white/10 font-mono text-xs text-rose-300 overflow-auto max-h-48">
              {this.state.error?.toString()}
            </div>

            <button
              onClick={() => {
                this.setState({ hasError: false, error: null, errorInfo: null });
                window.location.reload();
              }}
              className="w-full py-2.5 px-4 bg-indigo-600 hover:bg-indigo-500 text-white font-bold rounded-lg flex items-center justify-center gap-2 text-sm transition-colors"
            >
              <RefreshCw className="w-4 h-4" />
              Reload Interface
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

export default function App() {
  const [authState, setAuthState] = useState('checking'); // 'checking' | 'authenticated' | 'unauthenticated'
  const [user, setUser] = useState(null);

  // Check existing session on backend on app mount
  useEffect(() => {
    const checkSession = async () => {
      try {
        const res = await fetch('/api/status');
        if (res.ok) {
          const data = await res.json();
          if (data.authenticated) {
            setUser(data.user);
            setAuthState('authenticated');
            return;
          }
        }
      } catch (e) {
        console.warn('Backend session check error (backend might still be starting):', e);
      }
      setAuthState('unauthenticated');
    };

    checkSession();
  }, []);

  const handleLoginSuccess = (userData) => {
    setUser(userData);
    setAuthState('authenticated');
  };

  const handleLogout = () => {
    setUser(null);
    setAuthState('unauthenticated');
  };

  return (
    <ErrorBoundary>
      {authState === 'checking' ? (
        <div className="min-h-screen flex flex-col items-center justify-center gap-4 bg-[#0a0e17] text-slate-300">
          <Loader2 className="w-9 h-9 animate-spin text-indigo-400" />
          <div className="text-center space-y-1">
            <p className="text-sm font-semibold text-white">Checking Zerodha Kite Session...</p>
            <p className="text-xs text-slate-400">Verifying saved access token on backend</p>
          </div>
        </div>
      ) : authState === 'authenticated' ? (
        <Dashboard user={user} onLogout={handleLogout} />
      ) : (
        <LoginPage onLoginSuccess={handleLoginSuccess} />
      )}
    </ErrorBoundary>
  );
}
