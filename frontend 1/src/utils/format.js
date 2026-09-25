// Presentation-only helpers. These format values the backend already
// computed; they never derive new trading numbers.

export function formatCurrency(value, { compact = false } = {}) {
  if (value === null || value === undefined || Number.isNaN(value)) return 'N/A'
  const num = Number(value)
  if (compact && Math.abs(num) >= 100000) {
    return `₹${(num / 100000).toFixed(2)}L`
  }
  return `₹${num.toLocaleString('en-IN', { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`
}

export function formatNumber(value, decimals = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return 'N/A'
  return Number(value).toLocaleString('en-IN', {
    maximumFractionDigits: decimals,
    minimumFractionDigits: decimals,
  })
}

export function formatPercent(value, decimals = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return 'N/A'
  return `${Number(value) >= 0 ? '' : ''}${Number(value).toFixed(decimals)}%`
}

export function formatTimeIST(dateLike) {
  if (!dateLike) return 'N/A'
  const d = new Date(dateLike)
  if (Number.isNaN(d.getTime())) return 'N/A'
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(d)
}

export function formatDateTimeIST(dateLike) {
  if (!dateLike) return 'N/A'
  const d = new Date(dateLike)
  if (Number.isNaN(d.getTime())) return 'N/A'
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(d)
}

export function nowIST() {
  return formatTimeIST(new Date())
}

// value can be null/undefined; used to gate "N/A" everywhere a number
// is displayed so nothing silently renders as 0.
export function isPresent(value) {
  return value !== null && value !== undefined && !(typeof value === 'number' && Number.isNaN(value))
}
