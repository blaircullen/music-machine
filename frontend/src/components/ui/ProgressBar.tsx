interface ProgressBarProps {
  value?: number
  max?: number
  label?: string
  active?: boolean
}

export function ProgressBar({ value, max, label, active = false }: ProgressBarProps) {
  const hasValue = value !== undefined && max !== undefined && max > 0
  const pct = hasValue ? Math.min((value / max) * 100, 100) : 0

  return (
    <div className="space-y-1.5">
      {label && (
        <div className="flex items-center justify-between text-xs text-slate-400">
          <span>{label}</span>
          {hasValue && (
            <span className="font-mono text-slate-300">
              {value.toLocaleString()} / {max.toLocaleString()}
            </span>
          )}
        </div>
      )}
      <div className="h-2 w-full rounded-full bg-[#2a2d3a] overflow-hidden">
        {active && !hasValue ? (
          <div
            className="h-full w-1/3 rounded-full bg-[#0ea5e9] progress-shimmer"
            style={{
              backgroundImage:
                'linear-gradient(90deg, #0ea5e9 0%, #7dd3fc 50%, #0ea5e9 100%)',
              backgroundSize: '200% 100%',
              animation: 'progressSlide 1.4s ease-in-out infinite',
            }}
          />
        ) : (
          <div
            className="h-full rounded-full transition-all duration-500 ease-out"
            style={{
              width: `${pct}%`,
              background: active
                ? 'linear-gradient(90deg, #0ea5e9, #7dd3fc, #0ea5e9)'
                : '#0ea5e9',
              backgroundSize: active ? '200% 100%' : undefined,
              animation: active ? 'shimmerBar 1.8s linear infinite' : undefined,
            }}
          />
        )}
      </div>
    </div>
  )
}
