import { Component } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error }
  }

  componentDidCatch(error, info) {
    console.error('Unhandled UI error:', error, info)
  }

  render() {
    if (!this.state.hasError) return this.props.children
    return (
      <div className="min-h-screen bg-[var(--bg)] text-[var(--text)] flex items-center justify-center p-6">
        <div className="max-w-md w-full rounded-xl border border-[var(--negative)]/30 bg-[var(--panel)] p-6 space-y-4">
          <div className="flex items-center gap-3 text-[var(--negative)]">
            <AlertTriangle className="w-6 h-6" />
            <h2 className="text-sm font-semibold text-[var(--text)]">Something went wrong</h2>
          </div>
          <p className="text-xs font-num text-[var(--text-dim)] bg-black/30 rounded-md p-3 overflow-auto max-h-40">
            {this.state.error?.toString()}
          </p>
          <button
            onClick={() => window.location.reload()}
            className="w-full flex items-center justify-center gap-2 rounded-md bg-[var(--accent-dim)] border border-[var(--accent)]/30 text-[var(--accent)] py-2 text-sm font-medium"
          >
            <RefreshCw className="w-4 h-4" />
            Reload
          </button>
        </div>
      </div>
    )
  }
}
