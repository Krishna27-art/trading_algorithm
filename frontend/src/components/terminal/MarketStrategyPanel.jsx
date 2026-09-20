import React, { useState } from 'react';
import { BarChart2, Compass, Layers } from 'lucide-react';

export default function MarketStrategyPanel({ telemetry }) {
  const [timeframe, setTimeframe] = useState('15m');
  const [hoveredBar, setHoveredBar] = useState(null);

  const symbol = telemetry?.symbol || 'NIFTY';
  const ltp = telemetry?.current_price || 24150.0;
  const changePts = telemetry?.price_change_pts || 0.0;
  const changePct = telemetry?.price_change_pct || 0.0;
  const vwap = telemetry?.vwap || 24120.0;
  const orbHigh = telemetry?.orb_high || 24180.0;
  const orbLow = telemetry?.orb_low || 24095.0;
  const orbWidth = telemetry?.orb_width || (orbHigh - orbLow);
  const volFilterPassed = telemetry?.volatility_filter_passed ?? true;
  const candles = telemetry?.chart_candles || [];

  const activeSignal = telemetry?.active_signal;
  const activeTrade = telemetry?.active_trade;

  // Chart Dimensions & Margins
  const chartHeight = 280;
  const chartWidth = 720;
  const padding = { top: 25, right: 85, bottom: 30, left: 15 };

  const allPrices = [
    ...candles.map((c) => c.high),
    ...candles.map((c) => c.low),
    orbHigh,
    orbLow,
    vwap,
    activeSignal?.target,
    activeSignal?.stop_loss,
    activeSignal?.entry,
    activeTrade?.target,
    activeTrade?.stop_loss,
    activeTrade?.entry_price,
  ].filter(Boolean);

  const minPrice = allPrices.length > 0 ? Math.min(...allPrices) - 15 : 24000;
  const maxPrice = allPrices.length > 0 ? Math.max(...allPrices) + 15 : 24300;
  const priceRange = maxPrice - minPrice || 100;

  const getY = (price) => {
    return (
      padding.top +
      (1 - (price - minPrice) / priceRange) * (chartHeight - padding.top - padding.bottom)
    );
  };

  const getX = (idx, total) => {
    const usableWidth = chartWidth - padding.left - padding.right;
    if (total <= 1) return padding.left + usableWidth / 2;
    return padding.left + (idx / (total - 1)) * usableWidth;
  };

  // Generate VWAP Polyline Path
  const vwapPoints = candles
    .map((c, idx) => `${getX(idx, candles.length)},${getY(c.vwap)}`)
    .join(' ');

  const entryP = activeTrade?.entry_price || activeSignal?.entry;
  const targetP = activeTrade?.target || activeSignal?.target;
  const slP = activeTrade?.stop_loss || activeSignal?.stop_loss;

  return (
    <div className="term-panel p-4 flex flex-col justify-between space-y-3">
      {/* Chart Top Header: Symbol, Strategy, Timeframe Selector */}
      <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-white/[0.08]">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <h2 className="text-base sm:text-lg font-extrabold text-white tracking-tight">
              {symbol} 50 Intraday Chart
            </h2>
            <span className="text-xs font-mono font-bold text-indigo-300 bg-indigo-500/10 px-2 py-0.5 rounded border border-indigo-500/20">
              ORB + VWAP
            </span>
          </div>

          <div className="hidden sm:flex items-baseline gap-1.5 font-mono">
            <span className="text-sm font-bold text-white">₹{ltp.toFixed(2)}</span>
            <span
              className={`text-xs font-bold ${
                changePts >= 0 ? 'text-emerald-400' : 'text-rose-400'
              }`}
            >
              ({changePts >= 0 ? '+' : ''}
              {changePct.toFixed(2)}%)
            </span>
          </div>
        </div>

        {/* Timeframe Selector */}
        <div className="flex items-center gap-1 bg-black/40 p-1 rounded-lg border border-white/10 text-xs font-mono font-bold">
          {['1m', '5m', '15m'].map((tf) => (
            <button
              key={tf}
              onClick={() => setTimeframe(tf)}
              className={`px-3 py-1 rounded-md transition-all ${
                timeframe === tf
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-white hover:bg-white/5'
              }`}
            >
              {tf}
            </button>
          ))}
        </div>
      </div>

      {/* Professional Candlestick Chart Area */}
      <div className="relative w-full h-[280px] bg-[#070b14] rounded-lg border border-white/[0.06] overflow-hidden select-none">
        {candles.length === 0 ? (
          <div className="flex items-center justify-center h-full text-sm text-slate-400 font-mono">
            Waiting for real-time market candle stream...
          </div>
        ) : (
          <svg
            viewBox={`0 0 ${chartWidth} ${chartHeight}`}
            className="w-full h-full"
            preserveAspectRatio="none"
          >
            {/* Background Grid Lines & Y-Axis Labels */}
            {[0.15, 0.35, 0.55, 0.75, 0.95].map((ratio) => {
              const y = padding.top + ratio * (chartHeight - padding.top - padding.bottom);
              const p = maxPrice - ratio * priceRange;
              return (
                <g key={ratio}>
                  <line
                    x1={padding.left}
                    y1={y}
                    x2={chartWidth - padding.right}
                    y2={y}
                    stroke="rgba(255,255,255,0.04)"
                    strokeDasharray="4 4"
                  />
                  <text
                    x={chartWidth - padding.right + 8}
                    y={y + 4}
                    fill="#64748b"
                    fontSize="11"
                    fontFamily="monospace"
                    fontWeight="500"
                  >
                    {p.toFixed(0)}
                  </text>
                </g>
              );
            })}

            {/* OR High Line (Emerald Dashed) */}
            {orbHigh && (
              <g>
                <line
                  x1={padding.left}
                  y1={getY(orbHigh)}
                  x2={chartWidth - padding.right}
                  y2={getY(orbHigh)}
                  stroke="#10b981"
                  strokeWidth="1.5"
                  strokeDasharray="5 4"
                />
                <rect
                  x={chartWidth - padding.right + 2}
                  y={getY(orbHigh) - 8}
                  width="78"
                  height="16"
                  rx="3"
                  fill="rgba(16, 185, 129, 0.25)"
                  stroke="#10b981"
                  strokeWidth="0.75"
                />
                <text
                  x={chartWidth - padding.right + 5}
                  y={getY(orbHigh) + 4}
                  fill="#34d399"
                  fontSize="10"
                  fontFamily="monospace"
                  fontWeight="bold"
                >
                  ORH {orbHigh.toFixed(0)}
                </text>
              </g>
            )}

            {/* OR Low Line (Rose Dashed) */}
            {orbLow && (
              <g>
                <line
                  x1={padding.left}
                  y1={getY(orbLow)}
                  x2={chartWidth - padding.right}
                  y2={getY(orbLow)}
                  stroke="#f43f5e"
                  strokeWidth="1.5"
                  strokeDasharray="5 4"
                />
                <rect
                  x={chartWidth - padding.right + 2}
                  y={getY(orbLow) - 8}
                  width="78"
                  height="16"
                  rx="3"
                  fill="rgba(244, 63, 94, 0.25)"
                  stroke="#f43f5e"
                  strokeWidth="0.75"
                />
                <text
                  x={chartWidth - padding.right + 5}
                  y={getY(orbLow) + 4}
                  fill="#fb7185"
                  fontSize="10"
                  fontFamily="monospace"
                  fontWeight="bold"
                >
                  ORL {orbLow.toFixed(0)}
                </text>
              </g>
            )}

            {/* Target Line */}
            {targetP && (
              <g>
                <line
                  x1={padding.left}
                  y1={getY(targetP)}
                  x2={chartWidth - padding.right}
                  y2={getY(targetP)}
                  stroke="#34d399"
                  strokeWidth="1.5"
                  strokeDasharray="3 3"
                />
                <rect
                  x={chartWidth - padding.right + 2}
                  y={getY(targetP) - 8}
                  width="78"
                  height="16"
                  rx="3"
                  fill="rgba(52, 211, 153, 0.2)"
                  stroke="#34d399"
                  strokeWidth="0.75"
                />
                <text
                  x={chartWidth - padding.right + 5}
                  y={getY(targetP) + 4}
                  fill="#34d399"
                  fontSize="10"
                  fontFamily="monospace"
                  fontWeight="bold"
                >
                  TGT {targetP.toFixed(0)}
                </text>
              </g>
            )}

            {/* Entry Line */}
            {entryP && (
              <g>
                <line
                  x1={padding.left}
                  y1={getY(entryP)}
                  x2={chartWidth - padding.right}
                  y2={getY(entryP)}
                  stroke="#60a5fa"
                  strokeWidth="1.5"
                  strokeDasharray="4 4"
                />
                <rect
                  x={chartWidth - padding.right + 2}
                  y={getY(entryP) - 8}
                  width="78"
                  height="16"
                  rx="3"
                  fill="rgba(96, 165, 250, 0.2)"
                  stroke="#60a5fa"
                  strokeWidth="0.75"
                />
                <text
                  x={chartWidth - padding.right + 5}
                  y={getY(entryP) + 4}
                  fill="#93c5fd"
                  fontSize="10"
                  fontFamily="monospace"
                  fontWeight="bold"
                >
                  ENTRY {entryP.toFixed(0)}
                </text>
              </g>
            )}

            {/* Stop Loss Line */}
            {slP && (
              <g>
                <line
                  x1={padding.left}
                  y1={getY(slP)}
                  x2={chartWidth - padding.right}
                  y2={getY(slP)}
                  stroke="#fb7185"
                  strokeWidth="1.5"
                  strokeDasharray="3 3"
                />
                <rect
                  x={chartWidth - padding.right + 2}
                  y={getY(slP) - 8}
                  width="78"
                  height="16"
                  rx="3"
                  fill="rgba(251, 113, 133, 0.2)"
                  stroke="#fb7185"
                  strokeWidth="0.75"
                />
                <text
                  x={chartWidth - padding.right + 5}
                  y={getY(slP) + 4}
                  fill="#fb7185"
                  fontSize="10"
                  fontFamily="monospace"
                  fontWeight="bold"
                >
                  SL {slP.toFixed(0)}
                </text>
              </g>
            )}

            {/* Session VWAP (Cyan Polyline) */}
            {candles.length > 1 && (
              <polyline
                fill="none"
                stroke="#06b6d4"
                strokeWidth="2"
                points={vwapPoints}
                strokeOpacity="0.9"
              />
            )}

            {/* Candlesticks */}
            {candles.map((bar, idx) => {
              const x = getX(idx, candles.length);
              const candleW = Math.max(chartWidth / (candles.length * 2.1), 6);
              const isBull = bar.close >= bar.open;
              const topY = getY(Math.max(bar.open, bar.close));
              const botY = getY(Math.min(bar.open, bar.close));
              const bodyH = Math.max(botY - topY, 2);
              const color = isBull ? '#10b981' : '#f43f5e';

              return (
                <g
                  key={idx}
                  onMouseEnter={() => setHoveredBar(bar)}
                  onMouseLeave={() => setHoveredBar(null)}
                  className="cursor-pointer"
                >
                  {/* Wick */}
                  <line
                    x1={x}
                    y1={getY(bar.high)}
                    x2={x}
                    y2={getY(bar.low)}
                    stroke={color}
                    strokeWidth="1.2"
                  />
                  {/* Body */}
                  <rect
                    x={x - candleW / 2}
                    y={topY}
                    width={candleW}
                    height={bodyH}
                    fill={color}
                    rx="1"
                  />
                </g>
              );
            })}
          </svg>
        )}

        {/* Legend Overlay on Chart */}
        <div className="absolute top-3 left-4 flex flex-wrap items-center gap-3 text-xs font-mono bg-slate-950/80 px-3 py-1.5 rounded-md border border-white/10 backdrop-blur-md pointer-events-none">
          <span className="flex items-center gap-1.5 text-cyan-300 font-semibold">
            <span className="w-3 h-1 bg-cyan-400 inline-block rounded" /> VWAP ₹{vwap.toFixed(1)}
          </span>
          <span className="flex items-center gap-1.5 text-emerald-400 font-semibold">
            <span className="w-3 h-1 bg-emerald-400 border-t border-dashed inline-block" /> OR High ₹{orbHigh.toFixed(1)}
          </span>
          <span className="flex items-center gap-1.5 text-rose-400 font-semibold">
            <span className="w-3 h-1 bg-rose-400 border-t border-dashed inline-block" /> OR Low ₹{orbLow.toFixed(1)}
          </span>
          {hoveredBar && (
            <span className="text-slate-200 border-l border-white/20 pl-2">
              [{hoveredBar.time}] O:{hoveredBar.open} H:{hoveredBar.high} L:{hoveredBar.low} C:{hoveredBar.close}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
