import { useScan } from '../../hooks/ScanContext'
import { useUpgradeStatus } from '../../hooks/useUpgradeStatus'
import { Loader2, CheckCircle, Download, Search, AlertTriangle } from 'lucide-react'

const SCAN_PHASE_LABELS: Record<string, string> = {
  counting: 'Counting files...',
  scanning: 'Scanning',
  cleaning: 'Cleaning stale records...',
  analyzing: 'Analyzing duplicates...',
}

export function ActivityPanel() {
  const scan = useScan()
  const { status: upgrade } = useUpgradeStatus()

  if (scan.error) {
    return (
      <div className="flex items-center gap-2 text-xs text-red-400">
        <AlertTriangle className="w-3.5 h-3.5" />
        <span>Backend unreachable</span>
      </div>
    )
  }

  if (scan.running) {
    const phaseLabel = SCAN_PHASE_LABELS[scan.phase] ?? 'Working...'
    return (
      <div className="flex items-center gap-2 text-xs text-lime">
        <Loader2 className="w-3.5 h-3.5 animate-spin" />
        <span>
          {scan.phase === 'scanning' && scan.total > 0
            ? `Scanning: ${scan.progress.toLocaleString()}/${scan.total.toLocaleString()}`
            : phaseLabel
          }
        </span>
      </div>
    )
  }

  if (upgrade.phase === 'downloading' && upgrade.running) {
    const stepLabel = upgrade.current_step === 'transferring' ? '→ NAS'
      : upgrade.current_step === 'importing' ? '→ Library'
      : '↓ Soulseek'
    return (
      <div className="flex items-center gap-2 text-xs text-lime">
        <Download className="w-3.5 h-3.5 animate-bounce" />
        <span className="truncate max-w-[240px]">
          {upgrade.download_total > 0 && `${upgrade.download_index}/${upgrade.download_total} `}
          {stepLabel}
          {upgrade.current_track && ` — ${upgrade.current_track}`}
        </span>
      </div>
    )
  }

  if (upgrade.phase === 'searching' && upgrade.running) {
    return (
      <div className="flex items-center gap-2 text-xs text-lime">
        <Search className="w-3.5 h-3.5 animate-pulse" />
        <span>
          Searching: {upgrade.searched} searched, {upgrade.found} found
        </span>
      </div>
    )
  }

  return (
    <div className="flex items-center gap-2 text-xs text-base-500">
      <CheckCircle className="w-3.5 h-3.5" />
      <span>System idle</span>
    </div>
  )
}
