import { apiGet } from './client'

// GET /api/strategy/state?strategy= -> strategy metadata, schedule, risk config
export const getStrategyState = (strategy) =>
  apiGet(`/api/strategy/state${strategy ? `?strategy=${encodeURIComponent(strategy)}` : ''}`)

// Per-instrument freshness threshold. Mirrors the backend's own
// STREAM_DATA_STALE_AFTER_SECONDS (backend/stream_routes.py) so a row
// is flagged stale here using the same window the backend uses to
// decide overall stream freshness.
const INSTRUMENT_STALE_AFTER_SECONDS = 120

function instrumentFreshness(instrument) {
  if (instrument?.ltp === null || instrument?.ltp === undefined) {
    return 'DATA_UNAVAILABLE'
  }

  const tickTime = instrument?.last_tick_time || instrument?.updated_at
  if (!tickTime) {
    return 'DATA_UNAVAILABLE'
  }

  const parsed = new Date(tickTime)
  if (Number.isNaN(parsed.getTime())) {
    return 'DATA_UNAVAILABLE'
  }

  const ageSeconds = (Date.now() - parsed.getTime()) / 1000
  if (ageSeconds < 0 || ageSeconds > INSTRUMENT_STALE_AFTER_SECONDS) {
    return 'STALE'
  }

  return 'LIVE'
}

// GET /api/stream/market -> canonical streaming market state for the
// 700-stock universe (real Kite ticks only — see streaming/live_market_state.py).
//
// This adapts the canonical schema (symbol, ltp, open, high, low, close,
// volume, vwap, last_tick_time, data_source, plus rank/name/category
// enrichment from backend/stream_routes.py) to what the Stocks page
// renders. It never invents a value the canonical schema doesn't have:
//
//   - `change` / `change_pct` here are the intraday move from today's
//     session OPEN (ltp - open), because the canonical live stream has
//     no previous-close reference price to compute a vs-prev-close
//     change from. This is a real number derived from two real fields,
//     not a fabricated one — but it is a different metric than the old
//     vs-prev-close "change", so the Stocks page labels it accordingly
//     instead of presenting it under the old, now-inaccurate meaning.
//   - `status` is computed per-instrument from that instrument's own
//     last_tick_time, not assumed from overall stream health — a single
//     stock can be stale even while the rest of the feed is fine.
export const getMarketPrices = async () => {
  const data = await apiGet('/api/stream/market')
  const instruments = data?.instruments || {}

  const stocks = Object.values(instruments).map((instrument) => {
    const status = instrumentFreshness(instrument)
    const ltp = instrument?.ltp ?? null
    const open = instrument?.open ?? null

    const hasChangeBasis = status !== 'DATA_UNAVAILABLE' && ltp !== null && open !== null && open > 0
    const change = hasChangeBasis ? ltp - open : null
    const changePct = hasChangeBasis ? (change / open) * 100 : null

    return {
      ...instrument,
      status,
      ltp,
      open_price: open,
      change,
      change_pct: changePct,
    }
  })

  return {
    ...data,
    stocks,
    // The canonical stream only ever carries real Kite data (there is
    // no simulated/paper path here per system requirements), so when
    // the backend reports the equity feed fresh, the source is
    // genuinely REAL_KITE; otherwise it's the same real source, just
    // not currently fresh.
    data_source: data?.data_fresh ? 'REAL_KITE' : 'REAL_KITE_STALE',
  }
}