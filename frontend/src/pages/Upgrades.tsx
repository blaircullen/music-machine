import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import { ArrowUpCircle, Search, Download, CheckCircle, XCircle, Loader2, Server, HardDrive, PackageCheck, ChevronUp, ChevronDown, ChevronsUpDown } from 'lucide-react'
import { GlassCard, StatCard, Button, Badge, ProgressBar, EmptyState, SkeletonTable, toast } from '../components/ui'
import { useUpgradeStatus } from '../hooks/useUpgradeStatus'

interface QueueItem {
  id: number
  track_id: number
  search_query: string
  status: string
  match_quality: string | null
  created_at: string
  artist: string
  title: string
  album: string
  format: string
  bitrate: number
}

type FilterTab = 'all' | 'found' | 'approved' | 'completed' | 'skipped'
type SortCol = 'artist' | 'album' | 'title' | 'format' | 'match_quality' | 'status' | 'actions'
type SortDir = 'asc' | 'desc'

const STATUS_RANK: Record<string, number> = {
  found: 0, pending: 0, approved: 1, downloading: 2, completed: 3, searching: 4, skipped: 5, failed: 6,
}

const matchVariant = (m: string | null) => {
  if (m === 'hi_res') return 'green' as const
  if (m === 'lossless') return 'blue' as const
  return 'default' as const
}

const statusVariant = (s: string) => {
  const map: Record<string, 'default' | 'blue' | 'amber' | 'green' | 'red' | 'gray'> = {
    pending: 'default', approved: 'blue', downloading: 'amber',
    completed: 'green', failed: 'red', skipped: 'gray',
  }
  return map[s] ?? 'default'
}

export default function Upgrades() {
  const [queue, setQueue] = useState<QueueItem[]>([])
  const [loading, setLoading] = useState(true)
  const [filterTab, setFilterTab] = useState<FilterTab>('all')
  const [actionInProgress, setActionInProgress] = useState<Set<number>>(new Set())
  const [recentlyApproved, setRecentlyApproved] = useState<Set<number>>(new Set())
  const [downloadRequested, setDownloadRequested] = useState(false)
  const [searchRequested, setSearchRequested] = useState(false)
  const [sortCol, setSortCol] = useState<SortCol>('actions')
  const [sortDir, setSortDir] = useState<SortDir>('asc')

  const { status: upgradeStatus } = useUpgradeStatus()
  const prevPhaseRef = useRef(upgradeStatus.phase)

  const handleSort = (col: SortCol) => {
    if (col === sortCol) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    } else {
      setSortCol(col)
      setSortDir('asc')
    }
  }

  const sortedQueue = useMemo(() => {
    const q = [...queue]
    q.sort((a, b) => {
      let cmp = 0
      if (sortCol === 'actions') {
        cmp = (STATUS_RANK[a.status] ?? 9) - (STATUS_RANK[b.status] ?? 9)
      } else if (sortCol === 'format') {
        cmp = `${a.format}${a.bitrate}`.localeCompare(`${b.format}${b.bitrate}`)
      } else {
        const av = (a[sortCol] ?? '') as string
        const bv = (b[sortCol] ?? '') as string
        cmp = av.localeCompare(bv)
      }
      return sortDir === 'asc' ? cmp : -cmp
    })
    return q
  }, [queue, sortCol, sortDir])

  const isDownloading = (upgradeStatus.phase === 'downloading' && upgradeStatus.running) || downloadRequested
  const isSearching = (upgradeStatus.phase === 'searching' && upgradeStatus.running) || searchRequested

  const fetchQueue = useCallback(async () => {
    setLoading(true)
    try {
      const params = filterTab === 'all' ? '' : `?status=${filterTab}`
      const res = await fetch(`/api/upgrades${params}`)
      const data: QueueItem[] = await res.json()
      setQueue(data)
    } catch {
      toast.error('Failed to load upgrade queue')
      setQueue([])
    } finally {
      setLoading(false)
    }
  }, [filterTab])

  useEffect(() => {
    fetchQueue()
  }, [fetchQueue])

  // React to phase transitions
  useEffect(() => {
    const prev = prevPhaseRef.current
    prevPhaseRef.current = upgradeStatus.phase

    // Clear local request flags once backend confirms
    if (upgradeStatus.phase === 'downloading') setDownloadRequested(false)
    if (upgradeStatus.phase === 'searching') setSearchRequested(false)

    // Phase just ended → refresh queue
    const justFinished = !upgradeStatus.running && prev !== upgradeStatus.phase
    if (justFinished && (upgradeStatus.phase === 'complete' || upgradeStatus.phase === 'failed' || upgradeStatus.phase === 'idle')) {
      if (prev === 'searching') {
        const n = upgradeStatus.found
        if (n === 0) {
          toast.success('Search complete — nothing to upgrade right now')
        } else {
          toast.success(`Found ${n} potential upgrade${n === 1 ? '' : 's'}`)
        }
      }
      if (prev === 'downloading') {
        const n = upgradeStatus.completed
        const f = upgradeStatus.failed
        if (n === 0 && f > 0) {
          toast.error(`All ${f} download${f === 1 ? '' : 's'} failed — check Job Log`)
        } else if (f > 0) {
          toast.success(`${n} upgraded to FLAC, ${f} failed`)
        } else if (n === 1) {
          toast.success('One track upgraded to lossless')
        } else {
          toast.success(`${n} tracks upgraded to lossless`)
        }
      }
      setDownloadRequested(false)
      setSearchRequested(false)
      fetchQueue()
    }

  }, [upgradeStatus.phase, upgradeStatus.running, fetchQueue])

  const handleScan = async () => {
    setSearchRequested(true)
    try {
      await fetch('/api/upgrades/search', { method: 'POST' })
      toast.success('Search started')
    } catch {
      setSearchRequested(false)
      toast.error('Failed to start upgrade scan')
    }
  }

  const handleApprove = async (id: number, artist: string, title: string) => {
    setActionInProgress(prev => new Set(prev).add(id))
    try {
      const res = await fetch(`/api/upgrades/${id}/approve`, { method: 'POST' })
      const data = await res.json()
      if (data.error) {
        toast.error(data.error)
        return
      }
      setQueue(prev => prev.map(item =>
        item.id === id ? { ...item, status: 'approved' } : item
      ))
      setRecentlyApproved(prev => new Set(prev).add(id))
      setTimeout(() => setRecentlyApproved(prev => { const next = new Set(prev); next.delete(id); return next }), 2000)
      toast.success(`Approved: ${artist} - ${title}`)
    } catch {
      toast.error('Failed to approve')
    } finally {
      setActionInProgress(prev => { const next = new Set(prev); next.delete(id); return next })
    }
  }

  const handleSkip = async (id: number, artist: string, title: string) => {
    setActionInProgress(prev => new Set(prev).add(id))
    try {
      await fetch(`/api/upgrades/${id}/skip`, { method: 'POST' })
      setQueue(prev => prev.filter(item => item.id !== id))
      toast(`Skipped: ${artist} - ${title}`, { icon: '⏭' })
    } catch {
      toast.error('Failed to skip')
    } finally {
      setActionInProgress(prev => { const next = new Set(prev); next.delete(id); return next })
    }
  }

  const handleApproveAllExact = async () => {
    const count = foundCount
    try {
      await fetch('/api/upgrades/approve-all', { method: 'POST' })
      setQueue(prev => prev.map(item =>
        (item.status === 'found' || item.status === 'pending')
          ? { ...item, status: 'approved' }
          : item
      ))
      toast.success(`Approved ${count} found upgrades`)
    } catch {
      toast.error('Failed to approve all exact')
    }
  }

  const handleDownloadApproved = async () => {
    setDownloadRequested(true)
    try {
      const res = await fetch('/api/upgrades/download', { method: 'POST' })
      const data = await res.json()
      if (data.error) {
        setDownloadRequested(false)
        toast.error(data.error)
        return
      }
      toast.success(`Download started for ${data.count} tracks`)
    } catch {
      setDownloadRequested(false)
      toast.error('Failed to start downloads')
    }
  }

  const totalCandidates = queue.length
  const hiResMatches = queue.filter(i => i.match_quality === 'hi_res').length
  const approvedCount = queue.filter(i => i.status === 'approved').length
  const foundCount = queue.filter(i => i.status === 'found' || i.status === 'pending').length

  const tabs: { key: FilterTab; label: string; count: number }[] = [
    { key: 'all', label: 'All', count: queue.length },
    { key: 'found', label: 'Found', count: foundCount },
    { key: 'approved', label: 'Approved', count: approvedCount },
    { key: 'completed', label: 'Completed', count: queue.filter(i => i.status === 'completed').length },
    { key: 'skipped', label: 'Skipped', count: queue.filter(i => i.status === 'skipped').length },
  ]

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold font-[family-name:var(--font-family-display)]">Upgrades</h2>
        <div className="flex gap-3">
          {approvedCount > 0 && !isDownloading && (
            <button
              onClick={handleDownloadApproved}
              disabled={isDownloading}
              className="px-4 py-2 rounded-xl text-sm font-semibold transition-all duration-200 inline-flex items-center gap-2 bg-lime text-white hover:bg-lime/90 disabled:opacity-60"
            >
              <Download className="w-4 h-4" />
              Download Approved ({approvedCount})
            </button>
          )}
          {foundCount > 0 && (
            <Button variant="secondary" onClick={handleApproveAllExact}>
              <CheckCircle className="w-4 h-4" />
              Approve All Found ({foundCount})
            </Button>
          )}
          <button
            onClick={handleScan}
            disabled={isSearching || isDownloading}
            className="px-4 py-2 rounded-xl text-sm font-medium transition-all duration-300 inline-flex items-center gap-2 bg-base-700/80 text-base-300 hover:bg-base-600 border border-glass-border backdrop-blur-md disabled:opacity-40 disabled:cursor-not-allowed shadow-sm hover:shadow-md"
          >
            {isSearching ? <Loader2 className="w-4 h-4 animate-spin text-lime" /> : <Search className="w-4 h-4 text-lime" />}
            {isSearching ? 'Searching...' : 'Find Upgrades'}
          </button>
        </div>
      </div>

      {/* Search progress panel */}
      {isSearching && (
        <GlassCard className="p-5 border-blue-500/20 shadow-[0_0_20px_rgba(59,130,246,0.1)]">
          <ProgressBar
            value={upgradeStatus.searched}
            max={0}
            label={`Searching for upgrades… ${upgradeStatus.searched} searched, ${upgradeStatus.found} found`}
            active
          />
        </GlassCard>
      )}

      {/* Download progress panel */}
      {isDownloading && (
        <GlassCard className="p-5 border-lime/30 shadow-[0_0_20px_rgba(16,185,129,0.15)] relative overflow-hidden">
          <div className="absolute inset-0 bg-gradient-to-r from-transparent via-lime/5 to-transparent -translate-x-full animate-[shimmer_2s_infinite]" />

          {/* Header row */}
          <div className="flex items-center justify-between mb-4 relative z-10">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-xl bg-lime-dim border border-lime/20 shadow-[0_0_10px_rgba(16,185,129,0.2)]">
                <Download className="w-4 h-4 text-lime animate-bounce" />
              </div>
              <div>
                <p className="text-sm font-semibold text-base-200">Downloading FLAC upgrades</p>
                <p className="text-xs text-base-400">
                  {upgradeStatus.download_total > 0
                    ? `Track ${upgradeStatus.download_index} of ${upgradeStatus.download_total}`
                    : 'Starting downloads…'
                  }
                </p>
              </div>
            </div>
            {/* Counts */}
            <div className="flex items-center gap-4 text-xs text-base-400">
              {upgradeStatus.completed > 0 && (
                <span className="text-lime font-medium">✓ {upgradeStatus.completed} done</span>
              )}
              {upgradeStatus.failed > 0 && (
                <span className="text-red-400 font-medium">✗ {upgradeStatus.failed} failed</span>
              )}
            </div>
          </div>

          {/* Current track info */}
          {upgradeStatus.current_track && (
            <div className="mb-4 relative z-10 bg-base-800/50 rounded-xl p-3 border border-glass-border">
              <p className="text-xs text-base-400 mb-0.5">Now processing</p>
              <p className="text-sm font-semibold text-base-100 truncate">{upgradeStatus.current_track}</p>
              {upgradeStatus.current_album && (
                <p className="text-xs text-base-400 truncate mt-0.5">{upgradeStatus.current_album}</p>
              )}
            </div>
          )}

          {/* Step pipeline */}
          <div className="flex items-center gap-2 mb-4 relative z-10">
            {([
              { key: 'slskd',       label: 'Soulseek',    icon: Server },
              { key: 'transferring', label: 'Transfer → NAS', icon: HardDrive },
              { key: 'importing',   label: 'Import',      icon: PackageCheck },
            ] as const).map(({ key, label, icon: Icon }, i) => {
              const steps = ['slskd', 'transferring', 'importing'] as const
              const currentIdx = steps.indexOf(upgradeStatus.current_step ?? 'slskd')
              const stepIdx = steps.indexOf(key)
              const isDone = stepIdx < currentIdx
              const isActive = key === upgradeStatus.current_step
              return (
                <div key={key} className="flex items-center gap-2">
                  {i > 0 && <div className={`h-px w-6 ${isDone || isActive ? 'bg-lime/50' : 'bg-base-600'}`} />}
                  <div className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium transition-all duration-300 ${
                    isActive
                      ? 'bg-lime/15 text-lime border border-lime/30 shadow-[0_0_8px_rgba(16,185,129,0.2)]'
                      : isDone
                      ? 'bg-base-700/50 text-base-300 border border-base-600'
                      : 'bg-base-800/50 text-base-500 border border-base-700'
                  }`}>
                    {isActive ? <Loader2 className="w-3 h-3 animate-spin" /> : <Icon className="w-3 h-3" />}
                    {label}
                  </div>
                </div>
              )
            })}
          </div>

          {/* Byte-level progress (only during slskd download) */}
          {upgradeStatus.current_step === 'slskd' && upgradeStatus.current_total_bytes > 0 && (
            <div className="mb-3 relative z-10">
              <div className="flex justify-between text-xs text-base-400 mb-1">
                <span>{(upgradeStatus.current_bytes / 1024 / 1024).toFixed(1)} MB</span>
                <span>{(upgradeStatus.current_total_bytes / 1024 / 1024).toFixed(1)} MB</span>
              </div>
              <ProgressBar
                value={upgradeStatus.current_bytes}
                max={upgradeStatus.current_total_bytes}
                active
              />
            </div>
          )}

          {/* Overall progress bar */}
          {upgradeStatus.download_total > 0 && (
            <div className="relative z-10">
              <div className="flex justify-between text-xs text-base-500 mb-1">
                <span>Overall</span>
                <span>{upgradeStatus.completed + upgradeStatus.failed} / {upgradeStatus.download_total}</span>
              </div>
              <ProgressBar
                value={upgradeStatus.completed + upgradeStatus.failed}
                max={upgradeStatus.download_total}
                active={false}
              />
            </div>
          )}
        </GlassCard>
      )}

      <div className="grid grid-cols-3 gap-4">
        <StatCard icon={ArrowUpCircle} label="Candidates" value={totalCandidates.toLocaleString()} />
        <StatCard icon={CheckCircle} label="Hi-Res Found" value={hiResMatches.toLocaleString()} />
        <StatCard icon={Download} label="Approved" value={approvedCount.toLocaleString()} />
      </div>

      <div className="flex gap-2">
        {tabs.map(tab => (
          <button
            key={tab.key}
            onClick={() => setFilterTab(tab.key)}
            className={`px-3 py-1.5 rounded-xl text-sm font-medium transition-all duration-300 flex items-center gap-2 relative ${filterTab === tab.key
                ? 'text-lime drop-shadow-[0_0_8px_rgba(16,185,129,0.5)]'
                : 'text-base-500 hover:text-base-300 hover:bg-base-700/50'
              }`}
          >
            {tab.label}
            {tab.count > 0 && (
              <span className={`text-xs px-1.5 py-0.5 rounded-md ${filterTab === tab.key ? 'bg-lime/20 text-lime font-bold' : 'bg-base-800/80'}`}>{tab.count}</span>
            )}
            {/* Active Indiciator Line */}
            {filterTab === tab.key && (
              <motion.div layoutId="activeTabIndicator" className="absolute -bottom-1 left-3 right-3 h-0.5 bg-lime rounded-full shadow-[0_0_8px_rgba(16,185,129,0.8)]" />
            )}
          </button>
        ))}
      </div>

      {loading ? (
        <SkeletonTable rows={6} cols={7} />
      ) : queue.length === 0 ? (
        <EmptyState
          icon={ArrowUpCircle}
          title="No upgrades found"
          description="Click Find Upgrades to scan your library for quality improvements."
        />
      ) : (
        <GlassCard className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-glass-border text-base-400 text-left">
                  {(
                    [
                      { col: 'artist' as SortCol, label: 'Artist', align: 'left' },
                      { col: 'album' as SortCol, label: 'Album', align: 'left' },
                      { col: 'title' as SortCol, label: 'Title', align: 'left' },
                      { col: 'format' as SortCol, label: 'Format', align: 'left' },
                      { col: 'match_quality' as SortCol, label: 'Match', align: 'center' },
                      { col: 'status' as SortCol, label: 'Status', align: 'center' },
                      { col: 'actions' as SortCol, label: 'Actions', align: 'right' },
                    ] as const
                  ).map(({ col, label, align }) => {
                    const active = sortCol === col
                    const Icon = active ? (sortDir === 'asc' ? ChevronUp : ChevronDown) : ChevronsUpDown
                    return (
                      <th
                        key={col}
                        onClick={() => handleSort(col)}
                        className={`px-4 py-3 font-medium cursor-pointer select-none hover:text-base-200 transition-colors duration-150 ${align === 'center' ? 'text-center' : align === 'right' ? 'text-right' : ''}`}
                      >
                        <span className={`inline-flex items-center gap-1 ${align === 'center' ? 'justify-center' : align === 'right' ? 'justify-end' : ''}`}>
                          {align === 'right' && <Icon className={`w-3.5 h-3.5 ${active ? 'text-lime' : 'text-base-600'}`} />}
                          <span className={active ? 'text-lime' : ''}>{label}</span>
                          {align !== 'right' && <Icon className={`w-3.5 h-3.5 ${active ? 'text-lime' : 'text-base-600'}`} />}
                        </span>
                      </th>
                    )
                  })}
                </tr>
              </thead>
              <AnimatePresence>
                <tbody>
                  {sortedQueue.map(item => {
                    const busy = actionInProgress.has(item.id)
                    const canAct = item.status === 'found' || item.status === 'pending'
                    const justApproved = recentlyApproved.has(item.id)
                    return (
                      <motion.tr
                        key={item.id}
                        layout
                        initial={{ opacity: 0, y: 10 }}
                        animate={{
                          opacity: 1,
                          y: 0,
                          backgroundColor: justApproved ? 'var(--color-lime-dim)' : 'transparent',
                        }}
                        exit={{ opacity: 0, scale: 0.95, transition: { duration: 0.2 } }}
                        className="border-b border-glass-border/30 hover:bg-white/[0.02] transition-colors duration-200 group"
                      >
                        <td className="px-4 py-4 text-base-300 font-medium">{item.artist || '--'}</td>
                        <td className="px-4 py-4 text-base-400 group-hover:text-base-300 transition-colors">{item.album || '--'}</td>
                        <td className="px-4 py-4 text-base-300">{item.title || '--'}</td>
                        <td className="px-4 py-4 uppercase font-mono text-xs text-base-500 tracking-wider">
                          <span className="bg-base-700/80 px-1.5 py-0.5 rounded-md border border-base-600/50 shadow-sm">{item.format} {item.bitrate > 0 ? `${item.bitrate}k` : ''}</span>
                        </td>
                        <td className="px-4 py-3 text-center">
                          {item.match_quality
                            ? <Badge variant={matchVariant(item.match_quality)}>{item.match_quality}</Badge>
                            : <span className="text-base-600 text-xs">—</span>
                          }
                        </td>
                        <td className="px-4 py-3 text-center">
                          <Badge variant={statusVariant(item.status)}>{item.status}</Badge>
                        </td>
                        <td className="px-4 py-3 text-right">
                          {canAct && (
                            <div className="flex gap-2 justify-end">
                              <Button
                                size="sm"
                                variant="primary"
                                onClick={() => handleApprove(item.id, item.artist, item.title)}
                                disabled={busy}
                              >
                                Approve
                              </Button>
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => handleSkip(item.id, item.artist, item.title)}
                                disabled={busy}
                              >
                                <XCircle className="w-3.5 h-3.5" />
                              </Button>
                            </div>
                          )}
                        </td>
                      </motion.tr>
                    )
                  })}
                </tbody>
              </AnimatePresence>
            </table>
          </div>
        </GlassCard>
      )}
    </div>
  )
}
