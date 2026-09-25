import { formatTimeIST } from '../../utils/format'

// Shows when data was last fetched, and flags it as possibly stale once
// it's older than staleAfterMs. This never touches the values themselves.
export default function Timestamp({ updatedAt, staleAfterMs = 30000 }) {
  if (!updatedAt) return null
  const stale = Date.now() - updatedAt.getTime() > staleAfterMs
  return (
    <span className={`text-xs font-num ${stale ? 'text-[var(--warning)]' : 'text-[var(--text-faint)]'}`}>
      {stale ? 'Data may be stale · ' : 'Updated '}
      {formatTimeIST(updatedAt)} IST
    </span>
  )
}
