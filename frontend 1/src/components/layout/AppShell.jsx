import { useState } from 'react'
import { LayoutGrid, Radio, Wallet, History, Activity, Settings, LogOut, Menu, X } from 'lucide-react'

const NAV = [
  { id: 'dashboard', label: 'Dashboard', icon: LayoutGrid },
  { id: 'signals', label: 'Signals', icon: Radio },
  { id: 'positions', label: 'Positions', icon: Wallet },
  { id: 'backtest', label: 'Backtest', icon: History },
  { id: 'system', label: 'System', icon: Activity },
]

export default function AppShell({ active, onNavigate, onOpenSettings, onLogout, user, children }) {
  const [mobileOpen, setMobileOpen] = useState(false)

  return (
    <div className="min-h-screen bg-[var(--bg)] text-[var(--text)]">
      <header className="sticky top-0 z-30 border-b border-[var(--border)] bg-[var(--bg)]/95 backdrop-blur">
        <div className="mx-auto max-w-7xl px-4 sm:px-6">
          <div className="flex h-14 items-center justify-between">
            <div className="flex items-center gap-6">
              <div className="flex items-center gap-2">
                <div className="h-7 w-7 rounded-md bg-[var(--accent-dim)] border border-[var(--accent)]/30 flex items-center justify-center">
                  <Activity className="w-4 h-4 text-[var(--accent)]" />
                </div>
                <span className="font-semibold text-sm tracking-tight">Trading Terminal</span>
              </div>
              <nav className="hidden md:flex items-center gap-1">
                {NAV.map((item) => (
                  <NavButton
                    key={item.id}
                    item={item}
                    active={active === item.id}
                    onClick={() => onNavigate(item.id)}
                  />
                ))}
              </nav>
            </div>

            <div className="flex items-center gap-2">
              {user && (
                <span className="hidden sm:inline text-xs text-[var(--text-dim)] font-num">
                  {user.user_name}
                </span>
              )}
              <button
                onClick={onOpenSettings}
                className="p-2 rounded-md text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-white/[0.05] transition-colors"
                title="Settings"
              >
                <Settings className="w-4 h-4" />
              </button>
              <button
                onClick={onLogout}
                className="p-2 rounded-md text-[var(--text-dim)] hover:text-[var(--negative)] hover:bg-white/[0.05] transition-colors"
                title="Log out"
              >
                <LogOut className="w-4 h-4" />
              </button>
              <button
                onClick={() => setMobileOpen((v) => !v)}
                className="md:hidden p-2 rounded-md text-[var(--text-dim)] hover:bg-white/[0.05]"
              >
                {mobileOpen ? <X className="w-5 h-5" /> : <Menu className="w-5 h-5" />}
              </button>
            </div>
          </div>
        </div>

        {mobileOpen && (
          <nav className="md:hidden border-t border-[var(--border)] px-4 py-2 flex flex-col gap-1">
            {NAV.map((item) => (
              <NavButton
                key={item.id}
                item={item}
                active={active === item.id}
                onClick={() => {
                  onNavigate(item.id)
                  setMobileOpen(false)
                }}
                block
              />
            ))}
          </nav>
        )}
      </header>

      <main className="mx-auto max-w-7xl px-4 sm:px-6 py-5">{children}</main>
    </div>
  )
}

function NavButton({ item, active, onClick, block }) {
  const Icon = item.icon
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
        block ? 'w-full' : ''
      } ${
        active
          ? 'bg-[var(--accent-dim)] text-[var(--accent)]'
          : 'text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-white/[0.05]'
      }`}
    >
      <Icon className="w-4 h-4" />
      {item.label}
    </button>
  )
}
