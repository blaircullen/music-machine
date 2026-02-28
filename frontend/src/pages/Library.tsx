import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  ChevronRight,
  Check,
  X,
  Download,
  Search,
  Trash2,
  Copy,
  ArrowUpCircle,
  AlertCircle,
  Loader2,
  Server,
  HardDrive,
  PackageCheck,
  ChevronUp,
  ChevronDown,
  ChevronsUpDown,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { Button } from '../components/ui/Button'
import { Modal } from '../components/ui/Modal'
import { ProgressBar } from '../components/ui/ProgressBar'
import { FormatBadge, StatusBadge, QualityBadge } from '../components/ui/Badge'
import {
  getDupes,
  resolveDupe,
  resolveAllDupes,
  getUpgrades,
  getUpgradesStatus,
  postUpgradesSearch,
  postUpgradesDownload,
  approveUpgrade,
  approveAllUpgrades,
  skipUpgrade,
  type DupeGroup,
  type Track,
  type Upgrade,
  type UpgradeStatus,
} from '../lib/api'

// ─── helpers ────────────────────────────────────────────────────────────────

function fmtSampleRate(hz: number): string {
  return hz >= 1000 ? `${(hz / 1000).toFixed(1)} kHz` : `${hz} Hz`
}

function rankWithinGroup(track: Track, tracks: Track[]): number {
  const sorted = [...tracks].sort((a, b) => b.quality_score - a.quality_score)
  return sorted.findIndex((t) => t.id === track.id) + 1
}

// ─── Duplicates tab ──────────────────────────────────────────────────────────

type DupeFilter = 'all' | 'unresolved' | 'resolved'

function DuplicatesTab() {
  const [dupes, setDupes] = useState<DupeGroup[]>([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState<DupeFilter>('all')
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [optimisticResolved, setOptimisticResolved] = useState<Set<number>>(new Set())
  const [inFlight, setInFlight] = useState<Set<number>>(new Set())
  const [showBulkModal, setShowBulkModal] = useState(false)

  const fetchDupes = useCallback(async () => {
    setLoading(true)
    try {
      const data = await getDupes()
      setDupes(data)
    } catch {
      toast.error('Failed to load duplicates')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchDupes() }, [fetchDupes])

  const filtered = dupes.filter((g) => {
    const resolved = g.resolved || optimisticResolved.has(g.id)
    if (filter === 'unresolved') return !resolved
    if (filter === 'resolved') return resolved
    return true
  })

  const unresolvedCount = dupes.filter(
    (g) => !g.resolved && !optimisticResolved.has(g.id)
  ).length

  const handleResolve = async (id: number) => {
    // Optimistic hide
    setOptimisticResolved((prev) => new Set(prev).add(id))
    setInFlight((prev) => new Set(prev).add(id))
    try {
      const { moved } = await resolveDupe(id)
      toast.success(`Moved ${moved} file${moved !== 1 ? 's' : ''} to trash`)
      setDupes((prev) =>
        prev.map((g) => (g.id === id ? { ...g, resolved: true } : g))
      )
    } catch (err) {
      // Rollback
      setOptimisticResolved((prev) => { const next = new Set(prev); next.delete(id); return next })
      toast.error(err instanceof Error ? err.message : 'Failed to resolve group')
    } finally {
      setInFlight((prev) => { const next = new Set(prev); next.delete(id); return next })
    }
  }

  const handleResolveAll = async () => {
    setShowBulkModal(false)
    try {
      const { resolved } = await resolveAllDupes()
      toast.success(`Resolved ${resolved} groups`)
      await fetchDupes()
      setOptimisticResolved(new Set())
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to resolve all')
    }
  }

  const FILTERS: { key: DupeFilter; label: string }[] = [
    { key: 'all', label: 'All' },
    { key: 'unresolved', label: 'Unresolved' },
    { key: 'resolved', label: 'Resolved' },
  ]

  return (
    <div className="space-y-4">
      {/* Filter + bulk action */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex gap-1">
          {FILTERS.map(({ key, label }) => (
            <button
              key={key}
              onClick={() => setFilter(key)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                filter === key
                  ? 'bg-[#6c63ff]/15 text-[#a89fff] border border-[#6c63ff]/30'
                  : 'text-slate-500 hover:text-slate-300 hover:bg-[#2a2d3a]'
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {unresolvedCount > 0 && (
          <Button variant="danger" size="sm" onClick={() => setShowBulkModal(true)}>
            <Trash2 className="w-3.5 h-3.5" />
            Resolve All ({unresolvedCount})
          </Button>
        )}
      </div>

      <Modal
        open={showBulkModal}
        onClose={() => setShowBulkModal(false)}
        title="Resolve All Duplicates"
        message={`This will move losers from ${unresolvedCount} group${unresolvedCount !== 1 ? 's' : ''} to trash, keeping the highest quality version of each. Continue?`}
        confirmLabel="Move to Trash"
        confirmVariant="danger"
        onConfirm={handleResolveAll}
      />

      {loading ? (
        <div className="space-y-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="h-14 rounded-lg bg-[#1a1d27] border border-[#2a2d3a] animate-pulse" />
          ))}
        </div>
      ) : filtered.length === 0 ? (
        <div className="rounded-xl bg-[#1a1d27] border border-[#2a2d3a] p-12 text-center">
          <Copy className="w-8 h-8 text-slate-600 mx-auto mb-3" />
          <p className="text-slate-400 text-sm">No duplicate groups found</p>
          <p className="text-slate-600 text-xs mt-1">Run a scan to detect duplicates</p>
        </div>
      ) : (
        <div className="rounded-xl bg-[#1a1d27] border border-[#2a2d3a] overflow-hidden">
          <table className="w-full text-sm table-fixed">
            <thead>
              <tr className="border-b border-[#2a2d3a] text-xs text-slate-500 uppercase tracking-wide">
                <th className="px-4 py-3 text-left font-medium w-8" />
                <th className="px-4 py-3 text-left font-medium w-[22%]">Artist</th>
                <th className="px-4 py-3 text-left font-medium">Title</th>
                <th className="px-4 py-3 text-center font-medium w-[10%]">Match</th>
                <th className="px-4 py-3 text-center font-medium w-[10%]">Confidence</th>
                <th className="px-4 py-3 text-center font-medium w-[8%]">Members</th>
                <th className="px-4 py-3 text-right font-medium w-[12%]">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((group) => {
                const isResolved = group.resolved || optimisticResolved.has(group.id)
                const isFlying = inFlight.has(group.id)
                const isExpanded = expandedId === group.id
                const rep = group.tracks[0]

                return (
                  <>
                    <tr
                      key={group.id}
                      onClick={() => setExpandedId(isExpanded ? null : group.id)}
                      className={`border-b border-[#2a2d3a]/50 cursor-pointer transition-colors ${
                        isFlying
                          ? 'opacity-40'
                          : isResolved
                          ? 'opacity-50'
                          : 'hover:bg-white/[0.02]'
                      }`}
                    >
                      <td className="px-4 py-3.5 text-slate-500">
                        <ChevronRight
                          className={`w-4 h-4 transition-transform duration-200 ${isExpanded ? 'rotate-90' : ''}`}
                        />
                      </td>
                      <td className="px-4 py-3.5 text-slate-200 font-medium truncate max-w-0">
                        {rep?.artist || '—'}
                      </td>
                      <td className="px-4 py-3.5 text-slate-300 truncate max-w-0">{rep?.title || '—'}</td>
                      <td className="px-4 py-3.5 text-center">
                        <span className="text-xs bg-[#2a2d3a] px-2 py-0.5 rounded-full text-slate-400">
                          {group.match_type}
                        </span>
                      </td>
                      <td className="px-4 py-3.5 text-center text-slate-400 tabular-nums">
                        {Math.round(group.confidence * 100)}%
                      </td>
                      <td className="px-4 py-3.5 text-center">
                        <span className="text-xs bg-[#2a2d3a] px-2 py-0.5 rounded-full text-slate-400">
                          {group.tracks.length}
                        </span>
                      </td>
                      <td className="px-4 py-3.5 text-right" onClick={(e) => e.stopPropagation()}>
                        {isResolved ? (
                          <span className="inline-flex items-center gap-1 text-xs text-[#4ade80]">
                            <Check className="w-3.5 h-3.5" /> Resolved
                          </span>
                        ) : (
                          <Button
                            variant="danger"
                            size="sm"
                            loading={isFlying}
                            disabled={isFlying}
                            onClick={() => handleResolve(group.id)}
                          >
                            Resolve
                          </Button>
                        )}
                      </td>
                    </tr>

                    {isExpanded && (
                      <tr key={`${group.id}-expanded`} className="border-b border-[#2a2d3a]/50">
                        <td colSpan={7} className="p-0">
                          <div className="bg-[#13151f] border-t border-[#2a2d3a]/50 p-4">
                            <table className="w-full text-xs">
                              <thead>
                                <tr className="text-slate-600 uppercase tracking-wide border-b border-[#2a2d3a]/50">
                                  <th className="px-3 py-2 text-left font-medium">Status</th>
                                  <th className="px-3 py-2 text-left font-medium">Format</th>
                                  <th className="px-3 py-2 text-left font-medium">Bitrate</th>
                                  <th className="px-3 py-2 text-left font-medium">Bit Depth</th>
                                  <th className="px-3 py-2 text-left font-medium">Sample Rate</th>
                                  <th className="px-3 py-2 text-left font-medium">Path</th>
                                </tr>
                              </thead>
                              <tbody>
                                {[...group.tracks]
                                  .sort((a, b) => b.quality_score - a.quality_score)
                                  .map((track) => {
                                    const rank = rankWithinGroup(track, group.tracks)
                                    const isWinner = track.is_winner || rank === 1
                                    return (
                                      <tr
                                        key={track.id}
                                        className={`border-b border-[#2a2d3a]/30 ${
                                          isWinner ? 'bg-[#22c55e]/5' : ''
                                        }`}
                                      >
                                        <td className="px-3 py-2.5">
                                          {isWinner ? (
                                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-[#22c55e]/15 text-[#4ade80] border border-[#22c55e]/30 font-medium">
                                              <Check className="w-3 h-3" /> KEEP
                                            </span>
                                          ) : (
                                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-[#ef4444]/15 text-[#f87171] border border-[#ef4444]/30 font-medium">
                                              <X className="w-3 h-3" /> TRASH
                                            </span>
                                          )}
                                        </td>
                                        <td className="px-3 py-2.5">
                                          <FormatBadge format={track.format} />
                                        </td>
                                        <td className="px-3 py-2.5 font-mono text-slate-400">
                                          {track.bitrate > 0 ? `${track.bitrate} kbps` : '—'}
                                        </td>
                                        <td className="px-3 py-2.5 font-mono text-slate-400">
                                          {track.bit_depth ? `${track.bit_depth}-bit` : '—'}
                                        </td>
                                        <td className="px-3 py-2.5 font-mono text-slate-400">
                                          {track.sample_rate > 0
                                            ? fmtSampleRate(track.sample_rate)
                                            : '—'}
                                        </td>
                                        <td
                                          className="px-3 py-2.5 font-mono text-slate-500 max-w-xs truncate"
                                          title={track.path}
                                        >
                                          {track.path}
                                        </td>
                                      </tr>
                                    )
                                  })}
                              </tbody>
                            </table>
                          </div>
                        </td>
                      </tr>
                    )}
                  </>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ─── Upgrades tab ─────────────────────────────────────────────────────────────

type UpgradeFilter = 'all' | 'pending' | 'approved' | 'downloading' | 'completed' | 'failed' | 'skipped'

const UPGRADE_FILTERS: UpgradeFilter[] = [
  'all', 'pending', 'approved', 'downloading', 'completed', 'failed', 'skipped',
]

function UpgradesTab() {
  const [upgrades, setUpgrades] = useState<Upgrade[]>([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState<UpgradeFilter>('all')
  const [upgradeStatus, setUpgradeStatus] = useState<UpgradeStatus | null>(null)
  const [inFlight, setInFlight] = useState<Set<number>>(new Set())
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const [sortCol, setSortCol] = useState<'artist' | 'album' | 'title' | 'format' | 'match_quality' | 'status' | 'actions'>('actions')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc')

  const STATUS_RANK: Record<string, number> = { found: 0, pending: 0, approved: 1, downloading: 2, completed: 3, searching: 4, skipped: 5, failed: 6 }

  const handleUpgradeSort = (col: typeof sortCol) => {
    if (col === sortCol) setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    else { setSortCol(col); setSortDir('asc') }
  }

  const fetchUpgrades = useCallback(async () => {
    try {
      const data = await getUpgrades()
      setUpgrades(data)
    } catch {
      toast.error('Failed to load upgrades')
    } finally {
      setLoading(false)
    }
  }, [])

  const pollStatus = useCallback(async () => {
    try {
      const status = await getUpgradesStatus()
      setUpgradeStatus(status)
      // Refresh list when a task finishes
      if (!status.running) {
        fetchUpgrades()
      }
    } catch {
      // silently ignore
    }
  }, [fetchUpgrades])

  useEffect(() => {
    fetchUpgrades()
    pollStatus()
    pollRef.current = setInterval(pollStatus, 2000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [fetchUpgrades, pollStatus])

  const filtered = useMemo(() => {
    const f = upgrades.filter((u) => filter === 'all' || u.status === filter)
    f.sort((a, b) => {
      let cmp = 0
      if (sortCol === 'actions') cmp = (STATUS_RANK[a.status] ?? 9) - (STATUS_RANK[b.status] ?? 9)
      else if (sortCol === 'format') cmp = a.format.localeCompare(b.format)
      else cmp = ((a[sortCol as keyof typeof a] as string) ?? '').localeCompare((b[sortCol as keyof typeof b] as string) ?? '')
      return sortDir === 'asc' ? cmp : -cmp
    })
    return f
  }, [upgrades, filter, sortCol, sortDir])

  const pendingCount = upgrades.filter((u) => u.status === 'pending' || u.status === 'found').length
  const approvedCount = upgrades.filter((u) => u.status === 'approved').length

  const isSearching = upgradeStatus?.running && upgradeStatus.phase === 'searching'
  const isDownloading = upgradeStatus?.running && upgradeStatus.phase === 'downloading'
  const isRunning = upgradeStatus?.running

  const handleSearch = async () => {
    try {
      await postUpgradesSearch()
      toast.success('Soulseek search started')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to start search')
    }
  }

  const handleDownload = async () => {
    try {
      await postUpgradesDownload()
      toast.success('Download started')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to start download')
    }
  }

  const handleApprove = async (id: number) => {
    setInFlight((prev) => new Set(prev).add(id))
    try {
      await approveUpgrade(id)
      setUpgrades((prev) =>
        prev.map((u) => (u.id === id ? { ...u, status: 'approved' } : u))
      )
      toast.success('Approved')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to approve')
    } finally {
      setInFlight((prev) => { const next = new Set(prev); next.delete(id); return next })
    }
  }

  const handleApproveAll = async () => {
    try {
      const { approved } = await approveAllUpgrades()
      toast.success(`Approved ${approved} upgrades`)
      await fetchUpgrades()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to approve all')
    }
  }

  const handleSkip = async (id: number) => {
    setInFlight((prev) => new Set(prev).add(id))
    try {
      await skipUpgrade(id)
      setUpgrades((prev) =>
        prev.map((u) => (u.id === id ? { ...u, status: 'skipped' } : u))
      )
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to skip')
    } finally {
      setInFlight((prev) => { const next = new Set(prev); next.delete(id); return next })
    }
  }

  return (
    <div className="space-y-4">
      {/* Summary row */}
      <div className="grid grid-cols-4 gap-3">
        {[
          { label: 'Found', value: upgrades.filter((u) => u.status !== 'skipped').length },
          { label: 'Approved', value: approvedCount },
          { label: 'Completed', value: upgrades.filter((u) => u.status === 'completed').length },
          { label: 'Failed', value: upgrades.filter((u) => u.status === 'failed').length },
        ].map(({ label, value }) => (
          <div key={label} className="rounded-lg bg-[#1a1d27] border border-[#2a2d3a] px-4 py-3">
            <p className="text-xs text-slate-500">{label}</p>
            <p className="text-lg font-bold text-white tabular-nums">{value}</p>
          </div>
        ))}
      </div>

      {/* Progress bars */}
      {isSearching && (
        <div className="rounded-xl bg-[#1a1d27] border border-[#3b82f6]/30 p-4">
          <div className="flex items-center gap-2 mb-2">
            <Search className="w-4 h-4 text-[#60a5fa]" />
            <span className="text-sm text-[#60a5fa]">Searching Soulseek...</span>
          </div>
          <ProgressBar
            value={upgradeStatus?.searched}
            max={upgrades.filter((u) => u.status === 'pending').length || undefined}
            active
          />
        </div>
      )}

      {isDownloading && (
        <div className="rounded-xl bg-[#1a1d27] border border-[#6c63ff]/30 p-4 space-y-3">
          {/* Header */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Download className="w-4 h-4 text-[#a89fff] animate-bounce" />
              <span className="text-sm font-semibold text-[#a89fff]">Downloading FLACs</span>
              {upgradeStatus?.download_total ? (
                <span className="text-xs text-slate-500">
                  {upgradeStatus.download_index}/{upgradeStatus.download_total}
                </span>
              ) : null}
            </div>
            <div className="flex gap-3 text-xs">
              {(upgradeStatus?.completed ?? 0) > 0 && (
                <span className="text-emerald-400">✓ {upgradeStatus!.completed}</span>
              )}
              {(upgradeStatus?.failed ?? 0) > 0 && (
                <span className="text-red-400">✗ {upgradeStatus!.failed}</span>
              )}
            </div>
          </div>

          {/* Current track */}
          {upgradeStatus?.current_track && (
            <div className="bg-[#13151f] rounded-lg px-3 py-2 border border-[#2a2d3a]">
              <p className="text-[10px] text-slate-500 mb-0.5">Now processing</p>
              <p className="text-sm font-medium text-slate-200 truncate">{upgradeStatus.current_track}</p>
              {upgradeStatus.current_album && (
                <p className="text-xs text-slate-500 truncate">{upgradeStatus.current_album}</p>
              )}
            </div>
          )}

          {/* Step pipeline */}
          <div className="flex items-center gap-2">
            {([
              { key: 'slskd', label: 'Soulseek', Icon: Server },
              { key: 'transferring', label: '→ NAS', Icon: HardDrive },
              { key: 'importing', label: 'Import', Icon: PackageCheck },
            ] as const).map(({ key, label, Icon }, i) => {
              const steps = ['slskd', 'transferring', 'importing'] as const
              const currentIdx = steps.indexOf(upgradeStatus?.current_step ?? 'slskd')
              const stepIdx = steps.indexOf(key)
              const isDone = stepIdx < currentIdx
              const isActive = key === upgradeStatus?.current_step
              return (
                <div key={key} className="flex items-center gap-2">
                  {i > 0 && <div className={`h-px w-5 ${isDone || isActive ? 'bg-[#6c63ff]/50' : 'bg-[#2a2d3a]'}`} />}
                  <div className={`flex items-center gap-1.5 px-2 py-1 rounded-lg text-xs font-medium transition-all ${
                    isActive ? 'bg-[#6c63ff]/15 text-[#a89fff] border border-[#6c63ff]/40'
                    : isDone ? 'text-slate-400 border border-[#2a2d3a]'
                    : 'text-slate-600 border border-[#1e2030]'
                  }`}>
                    {isActive ? <Loader2 className="w-3 h-3 animate-spin" /> : <Icon className="w-3 h-3" />}
                    {label}
                  </div>
                </div>
              )
            })}
          </div>

          {/* Byte progress (slskd step only) */}
          {upgradeStatus?.current_step === 'slskd' && (upgradeStatus?.current_total_bytes ?? 0) > 0 && (
            <div>
              <div className="flex justify-between text-xs text-slate-500 mb-1">
                <span>{((upgradeStatus.current_bytes ?? 0) / 1024 / 1024).toFixed(1)} MB</span>
                <span>{((upgradeStatus.current_total_bytes ?? 0) / 1024 / 1024).toFixed(1)} MB</span>
              </div>
              <ProgressBar value={upgradeStatus.current_bytes} max={upgradeStatus.current_total_bytes} active />
            </div>
          )}

          {/* Overall progress */}
          {(upgradeStatus?.download_total ?? 0) > 0 && (
            <div>
              <div className="flex justify-between text-xs text-slate-500 mb-1">
                <span>Overall</span>
                <span>{(upgradeStatus!.completed) + (upgradeStatus!.failed)} / {upgradeStatus!.download_total}</span>
              </div>
              <ProgressBar
                value={(upgradeStatus?.completed ?? 0) + (upgradeStatus?.failed ?? 0)}
                max={upgradeStatus?.download_total ?? 1}
                active={false}
              />
            </div>
          )}
        </div>
      )}

      {/* Actions row */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex gap-1 flex-wrap">
          {UPGRADE_FILTERS.map((key) => {
            const count = key === 'all' ? upgrades.length : upgrades.filter((u) => u.status === key).length
            return (
              <button
                key={key}
                onClick={() => setFilter(key)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                  filter === key
                    ? 'bg-[#6c63ff]/15 text-[#a89fff] border border-[#6c63ff]/30'
                    : 'text-slate-500 hover:text-slate-300 hover:bg-[#2a2d3a]'
                }`}
              >
                {key.charAt(0).toUpperCase() + key.slice(1)}
                {count > 0 && (
                  <span className="ml-1.5 text-slate-600 tabular-nums">{count}</span>
                )}
              </button>
            )
          })}
        </div>

        <div className="flex gap-2 shrink-0">
          {pendingCount > 0 && (
            <Button variant="secondary" size="sm" onClick={handleApproveAll}>
              <Check className="w-3.5 h-3.5" />
              Approve All ({pendingCount})
            </Button>
          )}
          {approvedCount > 0 && (
            <Button
              variant="primary"
              size="sm"
              onClick={handleDownload}
              disabled={isRunning}
              loading={!!isDownloading}
            >
              <Download className="w-3.5 h-3.5" />
              Download Approved ({approvedCount})
            </Button>
          )}
          <Button
            variant="secondary"
            size="sm"
            onClick={handleSearch}
            disabled={isRunning}
            loading={!!isSearching}
          >
            <Search className="w-3.5 h-3.5" />
            Search Soulseek
          </Button>
        </div>
      </div>

      {/* Table */}
      {loading ? (
        <div className="space-y-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="h-14 rounded-lg bg-[#1a1d27] border border-[#2a2d3a] animate-pulse" />
          ))}
        </div>
      ) : filtered.length === 0 ? (
        <div className="rounded-xl bg-[#1a1d27] border border-[#2a2d3a] p-12 text-center">
          <ArrowUpCircle className="w-8 h-8 text-slate-600 mx-auto mb-3" />
          <p className="text-slate-400 text-sm">No upgrades in this filter</p>
          <p className="text-slate-600 text-xs mt-1">
            {filter === 'all' ? 'Search Soulseek to find FLAC upgrades' : `No ${filter} upgrades`}
          </p>
        </div>
      ) : (
        <div className="rounded-xl bg-[#1a1d27] border border-[#2a2d3a] overflow-hidden">
          <table className="w-full text-sm table-fixed">
            <thead>
              <tr className="border-b border-[#2a2d3a] text-xs text-slate-500 uppercase tracking-wide">
                {([
                  { col: 'artist', label: 'Artist', align: 'left', w: 'w-[18%]' },
                  { col: 'album', label: 'Album', align: 'left', w: 'w-[18%]' },
                  { col: 'title', label: 'Title', align: 'left', w: '' },
                  { col: 'format', label: 'Format', align: 'left', w: 'w-[8%]' },
                  { col: 'match_quality', label: 'Match', align: 'center', w: 'w-[8%]' },
                  { col: 'status', label: 'Status', align: 'center', w: 'w-[9%]' },
                  { col: 'actions', label: 'Actions', align: 'right', w: 'w-[12%]' },
                ] as const).map(({ col, label, align, w }) => {
                  const active = sortCol === col
                  const Icon = active ? (sortDir === 'asc' ? ChevronUp : ChevronDown) : ChevronsUpDown
                  return (
                    <th
                      key={col}
                      onClick={() => handleUpgradeSort(col)}
                      className={`px-4 py-3 font-medium cursor-pointer select-none hover:text-slate-300 transition-colors ${w} ${align === 'center' ? 'text-center' : align === 'right' ? 'text-right' : 'text-left'}`}
                    >
                      <span className={`inline-flex items-center gap-1 ${align === 'center' ? 'justify-center' : align === 'right' ? 'justify-end' : ''}`}>
                        {align === 'right' && <Icon className={`w-3 h-3 ${active ? 'text-[#4ade80]' : 'text-slate-600'}`} />}
                        <span className={active ? 'text-[#4ade80]' : ''}>{label}</span>
                        {align !== 'right' && <Icon className={`w-3 h-3 ${active ? 'text-[#4ade80]' : 'text-slate-600'}`} />}
                      </span>
                    </th>
                  )
                })}
              </tr>
            </thead>
            <tbody>
              {filtered.map((upgrade) => {
                const busy = inFlight.has(upgrade.id)
                return (
                  <tr
                    key={upgrade.id}
                    className="border-b border-[#2a2d3a]/50 hover:bg-white/[0.02] transition-colors"
                  >
                    <td className="px-4 py-3.5 text-slate-200 font-medium truncate max-w-0">{upgrade.artist || '—'}</td>
                    <td className="px-4 py-3.5 text-slate-400 truncate max-w-0">{upgrade.album || '—'}</td>
                    <td className="px-4 py-3.5 text-slate-300 truncate max-w-0">{upgrade.title || '—'}</td>
                    <td className="px-4 py-3.5">
                      <FormatBadge format={upgrade.format} />
                    </td>
                    <td className="px-4 py-3.5 text-center">
                      {upgrade.match_quality ? (
                        <QualityBadge quality={upgrade.match_quality} />
                      ) : (
                        <span className="text-slate-600">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3.5 text-center">
                      <StatusBadge status={upgrade.status} />
                    </td>
                    <td className="px-4 py-3.5 text-right">
                      {upgrade.status === 'failed' && upgrade.error_msg && (
                        <span
                          className="text-xs text-[#f87171] mr-3 max-w-[120px] inline-block truncate align-middle"
                          title={upgrade.error_msg}
                        >
                          <AlertCircle className="w-3 h-3 inline mr-1" />
                          {upgrade.error_msg}
                        </span>
                      )}
                      {upgrade.status === 'completed' && (
                        <Check className="w-4 h-4 text-[#4ade80] inline" />
                      )}
                      {(upgrade.status === 'pending' || upgrade.status === 'found') && (
                        <div className="flex gap-2 justify-end">
                          <Button
                            variant="primary"
                            size="sm"
                            loading={busy}
                            disabled={busy}
                            onClick={() => handleApprove(upgrade.id)}
                          >
                            Approve
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={busy}
                            onClick={() => handleSkip(upgrade.id)}
                          >
                            <X className="w-3.5 h-3.5" />
                          </Button>
                        </div>
                      )}
                      {upgrade.status === 'failed' && (
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={busy}
                          onClick={() => handleSkip(upgrade.id)}
                        >
                          Skip
                        </Button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ─── Library page ─────────────────────────────────────────────────────────────

type LibraryTab = 'duplicates' | 'upgrades'

export default function Library() {
  const [searchParams, setSearchParams] = useSearchParams()
  const tabParam = searchParams.get('tab')
  const activeTab: LibraryTab =
    tabParam === 'upgrades' ? 'upgrades' : 'duplicates'

  const setTab = (tab: LibraryTab) => {
    setSearchParams({ tab })
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-bold text-white">Library</h1>
        <p className="text-xs text-slate-500 mt-0.5">Manage duplicates and quality upgrades</p>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b border-[#2a2d3a] pb-0">
        {([
          { key: 'duplicates' as LibraryTab, label: 'Duplicates', icon: Copy },
          { key: 'upgrades' as LibraryTab, label: 'Upgrades', icon: ArrowUpCircle },
        ] as const).map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px ${
              activeTab === key
                ? 'text-[#a89fff] border-[#6c63ff]'
                : 'text-slate-500 border-transparent hover:text-slate-300 hover:border-[#2a2d3a]'
            }`}
          >
            <Icon className="w-4 h-4" />
            {label}
          </button>
        ))}
      </div>

      {activeTab === 'duplicates' ? <DuplicatesTab /> : <UpgradesTab />}
    </div>
  )
}
