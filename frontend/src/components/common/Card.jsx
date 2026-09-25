export default function Card({ title, action, children, className = '' }) {
  return (
    <div
      className={`rounded-xl border border-[var(--border)] bg-[var(--panel)] p-4 sm:p-5 ${className}`}
    >
      {(title || action) && (
        <div className="flex items-center justify-between mb-3">
          {title && <h2 className="text-sm font-semibold text-[var(--text)]">{title}</h2>}
          {action}
        </div>
      )}
      {children}
    </div>
  )
}
