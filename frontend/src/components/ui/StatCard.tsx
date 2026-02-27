import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'

interface StatCardProps {
  icon: LucideIcon
  label: string
  value: string | number
  subtitle?: string
  accent?: 'purple' | 'green' | 'amber' | 'red' | 'blue'
  children?: ReactNode
}

const ACCENT_CLASSES = {
  purple: {
    icon: 'bg-[#0ea5e9]/15 text-[#7dd3fc]',
    value: 'text-white',
  },
  green: {
    icon: 'bg-[#22c55e]/15 text-[#4ade80]',
    value: 'text-[#4ade80]',
  },
  amber: {
    icon: 'bg-[#f59e0b]/15 text-[#fbbf24]',
    value: 'text-[#fbbf24]',
  },
  red: {
    icon: 'bg-[#ef4444]/15 text-[#f87171]',
    value: 'text-[#f87171]',
  },
  blue: {
    icon: 'bg-[#3b82f6]/15 text-[#60a5fa]',
    value: 'text-[#60a5fa]',
  },
}

export function StatCard({ icon: Icon, label, value, subtitle, accent = 'purple', children }: StatCardProps) {
  const colors = ACCENT_CLASSES[accent]

  return (
    <div className="rounded-xl bg-[#1a1d27] border border-[#2a2d3a] p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="text-xs text-slate-500 uppercase tracking-wide mb-1.5">{label}</p>
          <p className={`text-2xl font-bold tabular-nums ${colors.value}`}>{value}</p>
          {subtitle && <p className="text-xs text-slate-500 mt-1">{subtitle}</p>}
          {children}
        </div>
        <div className={`shrink-0 rounded-lg p-2.5 ${colors.icon}`}>
          <Icon className="w-5 h-5" />
        </div>
      </div>
    </div>
  )
}
