import type { ReactNode } from 'react'

// Generic badge
type BadgeVariant =
  | 'default'
  | 'purple'
  | 'green'
  | 'amber'
  | 'red'
  | 'blue'
  | 'orange'
  | 'yellow'
  | 'gray'

const VARIANT_CLASSES: Record<BadgeVariant, string> = {
  default: 'bg-[#2a2d3a] text-slate-300 border border-[#3a3d4a]',
  purple: 'bg-[#6c63ff]/15 text-[#a89fff] border border-[#6c63ff]/30',
  green: 'bg-[#22c55e]/15 text-[#4ade80] border border-[#22c55e]/30',
  amber: 'bg-[#f59e0b]/15 text-[#fbbf24] border border-[#f59e0b]/30',
  red: 'bg-[#ef4444]/15 text-[#f87171] border border-[#ef4444]/30',
  blue: 'bg-[#3b82f6]/15 text-[#60a5fa] border border-[#3b82f6]/30',
  orange: 'bg-orange-500/15 text-orange-400 border border-orange-500/30',
  yellow: 'bg-yellow-500/15 text-yellow-400 border border-yellow-500/30',
  gray: 'bg-slate-700/50 text-slate-400 border border-slate-600/50',
}

interface BadgeProps {
  children: ReactNode
  variant?: BadgeVariant
  className?: string
}

export function Badge({ children, variant = 'default', className = '' }: BadgeProps) {
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${VARIANT_CLASSES[variant]} ${className}`}
    >
      {children}
    </span>
  )
}

// Format badge: picks color based on format string
const FORMAT_VARIANTS: Record<string, BadgeVariant> = {
  mp3: 'orange',
  flac: 'green',
  aac: 'yellow',
  alac: 'green',
  wav: 'blue',
  aiff: 'blue',
  ogg: 'amber',
  wma: 'gray',
  m4a: 'yellow',
}

interface FormatBadgeProps {
  format: string
}

export function FormatBadge({ format }: FormatBadgeProps) {
  const key = format.toLowerCase()
  const variant = FORMAT_VARIANTS[key] ?? 'default'
  return <Badge variant={variant}>{format.toUpperCase()}</Badge>
}

// Status badge: for upgrade/job statuses
const STATUS_VARIANTS: Record<string, BadgeVariant> = {
  pending: 'gray',
  found: 'blue',
  approved: 'purple',
  downloading: 'amber',
  completed: 'green',
  failed: 'red',
  skipped: 'gray',
  running: 'blue',
}

interface StatusBadgeProps {
  status: string
}

export function StatusBadge({ status }: StatusBadgeProps) {
  const variant = STATUS_VARIANTS[status.toLowerCase()] ?? 'default'
  return <Badge variant={variant}>{status}</Badge>
}

// Quality badge: for match_quality field on upgrades
interface QualityBadgeProps {
  quality: string
}

export function QualityBadge({ quality }: QualityBadgeProps) {
  const lower = quality.toLowerCase()
  let variant: BadgeVariant = 'default'
  if (lower.includes('hi-res') || lower.includes('hires')) variant = 'purple'
  else if (lower.includes('lossless')) variant = 'green'
  else if (lower.includes('high')) variant = 'blue'
  return <Badge variant={variant}>{quality}</Badge>
}
