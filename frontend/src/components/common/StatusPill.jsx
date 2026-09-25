const TONES = {
  positive: 'text-[var(--positive)] bg-[var(--positive-dim)] border-[var(--positive)]/30',
  negative: 'text-[var(--negative)] bg-[var(--negative-dim)] border-[var(--negative)]/30',
  warning: 'text-[var(--warning)] bg-[var(--warning-dim)] border-[var(--warning)]/30',
  neutral: 'text-[var(--text-dim)] bg-white/[0.04] border-[var(--border-strong)]',
  accent: 'text-[var(--accent)] bg-[var(--accent-dim)] border-[var(--accent)]/30',
}

export default function StatusPill({ tone = 'neutral', children, dot = true }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium ${TONES[tone]}`}
    >
      {dot && <span className={`h-1.5 w-1.5 rounded-full ${dotColor(tone)}`} />}
      {children}
    </span>
  )
}

function dotColor(tone) {
  switch (tone) {
    case 'positive':
      return 'bg-[var(--positive)]'
    case 'negative':
      return 'bg-[var(--negative)]'
    case 'warning':
      return 'bg-[var(--warning)]'
    case 'accent':
      return 'bg-[var(--accent)]'
    default:
      return 'bg-[var(--text-faint)]'
  }
}
