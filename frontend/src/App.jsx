import React, { useState, useEffect } from 'react';
import LoginPage from './components/LoginPage';
import Dashboard from './components/Dashboard';
import { Loader2 } from 'lucide-react';

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

  if (authState === 'checking') {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center gap-4 bg-[#0a0e17] text-slate-300">
        <Loader2 className="w-9 h-9 animate-spin text-indigo-400" />
        <div className="text-center space-y-1">
          <p className="text-sm font-semibold text-white">Checking Zerodha Kite Session...</p>
          <p className="text-xs text-slate-400">Verifying saved access token on backend</p>
        </div>
      </div>
    );
  }

  if (authState === 'authenticated') {
    return <Dashboard user={user} onLogout={handleLogout} />;
  }

  return <LoginPage onLoginSuccess={handleLoginSuccess} />;
}
