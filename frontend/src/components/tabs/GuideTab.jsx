import React from 'react';
import { Terminal, ShieldAlert, Cpu, Sparkles, BookOpen, ExternalLink, Code2, Copy, Check } from 'lucide-react';

export default function GuideTab() {
  const [copiedIndex, setCopiedIndex] = React.useState(null);

  const copyToClipboard = (text, index) => {
    navigator.clipboard.writeText(text);
    setCopiedIndex(index);
    setTimeout(() => setCopiedIndex(null), 2000);
  };

  const codeSnippets = [
    {
      title: '1. Start FastAPI Backend',
      cmd: 'source .venv/bin/activate\nuvicorn backend.main:app --reload --port 8000',
    },
    {
      title: '2. Start React + Vite Frontend',
      cmd: 'cd frontend\nnpm run dev',
    },
    {
      title: '3. Run Algo Execution Script directly via CLI',
      cmd: 'source .venv/bin/activate\npython strategy_template.py',
    },
  ];

  return (
    <div className="space-y-6 animate-fade-in max-w-5xl">
      {/* Introduction Card */}
      <div className="glass-panel p-6 border-indigo-500/20">
        <div className="flex items-center gap-3 mb-2">
          <div className="p-2 rounded-lg bg-indigo-500/10 text-indigo-400">
            <BookOpen className="w-5 h-5" />
          </div>
          <h2 className="text-xl font-bold text-white">Setup & Running Instructions</h2>
        </div>
        <p className="text-sm text-slate-300 leading-relaxed">
          Welcome to your semi-automated Zerodha Kite Connect trading architecture. The system separates the high-performance Python FastAPI backend from the sleek React dashboard, ensuring token security and seamless local development persistence.
        </p>
      </div>

      {/* Code / Command Cards */}
      <div className="space-y-4">
        <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wider">How to Run Locally</h3>
        <div className="grid grid-cols-1 gap-4">
          {codeSnippets.map((snippet, idx) => (
            <div key={idx} className="glass-panel p-5 space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-slate-200">{snippet.title}</span>
                <button
                  onClick={() => copyToClipboard(snippet.cmd, idx)}
                  className="btn-secondary text-[11px] py-1 px-2.5 flex items-center gap-1.5"
                >
                  {copiedIndex === idx ? (
                    <>
                      <Check className="w-3.5 h-3.5 text-emerald-400" />
                      <span className="text-emerald-400">Copied!</span>
                    </>
                  ) : (
                    <>
                      <Copy className="w-3.5 h-3.5" />
                      <span>Copy</span>
                    </>
                  )}
                </button>
              </div>
              <pre className="p-3.5 rounded-xl bg-black/50 border border-white/5 font-mono text-xs text-indigo-300 overflow-x-auto">
                <code>{snippet.cmd}</code>
              </pre>
            </div>
          ))}
        </div>
      </div>

      {/* Security Best Practices */}
      <div className="glass-panel p-6 space-y-3 border-emerald-500/20">
        <div className="flex items-center gap-2 text-emerald-400">
          <ShieldAlert className="w-5 h-5" />
          <h3 className="text-base font-bold text-white">Token Security & Local Persistence</h3>
        </div>
        <ul className="text-xs text-slate-300 space-y-2 list-disc pl-5 leading-relaxed">
          <li>
            <strong>Zero Frontend Token Leaks:</strong> The Zerodha <code className="text-emerald-300 bg-white/5 px-1 py-0.5 rounded">access_token</code> is strictly stored on the backend in <code className="text-indigo-300 bg-white/5 px-1 py-0.5 rounded">session_token.json</code> and never transmitted across the wire to the frontend browser.
          </li>
          <li>
            <strong>Developer Reload Persistence:</strong> The backend automatically reads and validates the active session on startup, so you never need to re-login after code changes or browser refreshes.
          </li>
          <li>
            <strong>Daily Reset:</strong> Zerodha expires all session tokens every day between 6:00 AM - 7:30 AM IST. Simply authenticate once every morning before market hours.
          </li>
        </ul>
      </div>
    </div>
  );
}
