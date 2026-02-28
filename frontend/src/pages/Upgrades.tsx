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

type FilterTab = 'all' | 'found' | 'approved' | 'completed' | 'skipped' | 'unscanned'
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
  const [reviewMode, setReviewMode] = useState(false)
  const [reviewIndex, setReviewIndex] = useState(0)

  const [coverage, setCoverage] = useState<{
    total_candidates: number
    scanned: number
    unscanned: number
    found: number
    completed: number
  } | null>(null)

  const [unscannedTracks, setUnscannedTracks] = useState<Array<{
    track_id: number
    artist: string
    album: string
    title: string
    format: string
    bitrate: number
  }>>([])

  const [scanModalOpen, setScanModalOpen] = useState(false)
  const [scanScope, setScanScope] = useState({
    format_filter: 'all_lossy',
    unscanned_only: true,
    batch_size: 50,
    artist_filter: '',
  })

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

  const fetchCoverage = useCallback(async () => {
    try {
      const res = await fetch('/api/upgrades/coverage')
      if (!res.ok) return
      setCoverage(await res.json())
    } catch {}
  }, [])

  const fetchUnscanned = useCallback(async () => {
    try {
      const res = await fetch('/api/upgrades/unscanned')
      if (!res.ok) return
      setUnscannedTracks(await res.json())
    } catch {}
  }, [])

  const fetchQueue = useCallback(async () => {
    setLoading(true)
    try {
      const params = filterTab === 'all' ? '' : `?status=${filterTab}`
      const res = await fetch(`/api/upgrades${params}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
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
    if (filterTab === 'unscanned') {
      fetchUnscanned()
      fetchCoverage()
    } else {
      fetchQueue()
      fetchCoverage()
    }
  }, [filterTab, fetchQueue, fetchCoverage, fetchUnscanned])

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
      fetchCoverage()
    }

  }, [upgradeStatus.phase, upgradeStatus.running, fetchQueue, fetchCoverage])

  const handleScan = async () => {
    setScanModalOpen(false)
    setSearchRequested(true)
    try {
      await fetch('/api/upgrades/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...scanScope,
          artist_filter: scanScope.artist_filter || null,
          batch_size: Number(scanScope.batch_size),
        }),
      })
      toast.success('Search started')
    } catch {
      setSearchRequested(false)
      toast.error('Failed to start upgrade scan')
    }
  }

  const handleScanAlbum = async (artist: string, _album: string) => {
    setSearchRequested(true)
    try {
      await fetch('/api/upgrades/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          format_filter: 'all_lossy',
          unscanned_only: false,
          batch_size: 5,
          artist_filter: artist || null,
        }),
      })
      toast.success(`Scanning: ${artist}`)
    } catch {
      setSearchRequested(false)
      toast.error('Failed to start scan')
    }
  }

  const handleApprove = useCallback(async (id: number, artist: string, title: string) => {
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
  }, [])

  const handleSkip = useCallback(async (id: number, artist: string, title: string) => {
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
  }, [])

  const handleApproveHiRes = async () => {
    try {
      const res = await fetch('/api/upgrades/approve-hi-res', { method: 'POST' })
      if (!res.ok) return
      const data = await res.json()
      setQueue(prev => prev.map(item =>
        item.status === 'found' && item.match_quality === 'hi_res'
          ? { ...item, status: 'approved' }
          : item
      ))
      toast.success(`Approved ${data.approved} hi-res match${data.approved === 1 ? '' : 'es'}`)
    } catch {
      toast.error('Failed to approve hi-res')
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

  const foundItems = useMemo(
    () => sortedQueue.filter(i => i.status === 'found' || i.status === 'pending'),
    [sortedQueue]
  )
  const reviewItem = foundItems[reviewIndex] ?? null

  // Clamp reviewIndex whenever foundItems shrinks
  useEffect(() => {
    if (foundItems.length > 0) {
      setReviewIndex(i => Math.min(i, foundItems.length - 1))
    }
  }, [foundItems.length])

  useEffect(() => {
    if (!reviewMode) return
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement).tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      if (e.key === 'a' || e.key === 'A' || e.key === ' ') {
        e.preventDefault()
        if (reviewItem && !actionInProgress.has(reviewItem.id)) {
          handleApprove(reviewItem.id, reviewItem.artist, reviewItem.title)
        }
      }
      if (e.key === 's' || e.key === 'S' || e.key === 'x' || e.key === 'X') {
        e.preventDefault()
        if (reviewItem && !actionInProgress.has(reviewItem.id)) {
          handleSkip(reviewItem.id, reviewItem.artist, reviewItem.title)
        }
      }
      if (e.key === 'ArrowRight') setReviewIndex(i => Math.min(i + 1, foundItems.length - 1))
      if (e.key === 'ArrowLeft') setReviewIndex(i => Math.max(i - 1, 0))
      if (e.key === 'Escape') setReviewMode(false)
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [reviewMode, reviewItem, foundItems.length, actionInProgress, handleApprove, handleSkip])

  const tabs: { key: FilterTab; label: string; count: number }[] = [
    { key: 'all', label: 'All', count: queue.length },
    { key: 'found', label: 'Found', count: foundCount },
    { key: 'approved', label: 'Approved', count: approvedCount },
    { key: 'completed', label: 'Completed', count: queue.filter(i => i.status === 'completed').length },
    { key: 'skipped', label: 'Skipped', count: queue.filter(i => i.status === 'skipped').length },
    { key: 'unscanned', label: 'Never Scanned', count: coverage?.unscanned ?? 0 },
  ]

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-2xl font-bold font-[family-name:var(--font-family-display)]">Upgrades</h2>
          {coverage && (
            <div className="text-xs text-base-500 flex gap-4 flex-wrap mt-1">
              <span>
                Coverage:{' '}
                <span className="text-base-300 font-medium">{coverage.scanned.toLocaleString()} scanned</span>
                {' · '}
                <button
                  className="text-amber-400 font-medium hover:underline"
                  onClick={() => setFilterTab('unscanned')}
                >
                  {coverage.unscanned.toLocaleString()} never scanned
                </button>
                {' · '}
                <span className="text-lime font-medium">{coverage.found.toLocaleString()} found</span>
                {' · '}
                <span className="text-base-400">{coverage.completed.toLocaleString()} completed</span>
              </span>
            </div>
          )}
        </div>
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
            onClick={() => setScanModalOpen(true)}
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

      <div className="flex items-center gap-2">
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
        {filterTab === 'found' && foundItems.length > 0 && (
          <div className="flex items-center gap-2 ml-auto">
            <button
              onClick={handleApproveHiRes}
              className="px-3 py-1.5 rounded-lg text-sm font-medium bg-green-900/40 text-green-400 border border-green-800/50 hover:bg-green-900/60 transition-all"
            >
              Approve all hi-res
            </button>
            <button
              onClick={() => { setReviewMode(m => !m); setReviewIndex(0) }}
              className={`px-3 py-1.5 rounded-lg text-sm font-medium border transition-all ${
                reviewMode
                  ? 'bg-lime/20 border-lime/40 text-lime'
                  : 'bg-base-700 border-base-600 text-base-400 hover:text-base-200'
              }`}
            >
              {reviewMode ? 'Exit Review' : 'Review Mode'}
            </button>
          </div>
        )}
      </div>

      {loading ? (
        <SkeletonTable rows={6} cols={7} />
      ) : filterTab === 'unscanned' ? (
        unscannedTracks.length === 0 ? (
          <EmptyState
            icon={CheckCircle}
            title="All candidates scanned"
            description="Every lossy and CD-FLAC track has been searched at least once."
          />
        ) : (
          <GlassCard className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-glass-border text-base-400 text-left">
                    <th className="px-4 py-3 font-medium">Artist</th>
                    <th className="px-4 py-3 font-medium">Album</th>
                    <th className="px-4 py-3 font-medium">Title</th>
                    <th className="px-4 py-3 font-medium">Format</th>
                    <th className="px-4 py-3 font-medium text-right">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {unscannedTracks.map(track => (
                    <tr key={track.track_id} className="border-b border-glass-border/30 hover:bg-white/[0.02]">
                      <td className="px-4 py-3 text-base-300 font-medium">{track.artist || '--'}</td>
                      <td className="px-4 py-3 text-base-400">{track.album || '--'}</td>
                      <td className="px-4 py-3 text-base-400">{track.title || '--'}</td>
                      <td className="px-4 py-3">
                        <span className="bg-base-700/80 px-1.5 py-0.5 rounded-md border border-base-600/50 font-mono text-xs uppercase">
                          {track.format} {track.bitrate > 0 ? `${track.bitrate}k` : ''}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right">
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => handleScanAlbum(track.artist, track.album)}
                        >
                          <Search className="w-3 h-3" />
                          Scan artist
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </GlassCard>
        )
      ) : filterTab === 'found' && reviewMode ? (
        foundItems.length === 0 ? (
          <EmptyState icon={CheckCircle} title="All reviewed" description="No more found items to review." />
        ) : (
          <div className="flex flex-col items-center gap-6">
            <p className="text-sm text-base-500">
              {reviewIndex + 1} of {foundItems.length} found
              <span className="ml-3 text-xs text-base-600">A/Space = approve · S/X = skip · ← → navigate · Esc = exit</span>
            </p>

            {reviewItem && (
              <GlassCard className="w-full max-w-2xl p-8">
                <div className="mb-1 text-base-400 text-sm font-medium">{reviewItem.artist || '—'}</div>
                <div className="text-xl font-bold text-base-100 mb-1">{reviewItem.album || '—'}</div>
                <div className="text-base-300 text-lg mb-6">{reviewItem.title || '—'}</div>

                <div className="flex gap-8 mb-8">
                  <div>
                    <p className="text-xs text-base-500 uppercase tracking-wider mb-1">Current</p>
                    <span className="font-mono text-sm bg-base-700/80 px-2 py-1 rounded-lg border border-base-600/50 uppercase">
                      {reviewItem.format} {reviewItem.bitrate > 0 ? `${reviewItem.bitrate}k` : ''}
                    </span>
                  </div>
                  <div className="text-base-500 self-end mb-2">→</div>
                  <div>
                    <p className="text-xs text-base-500 uppercase tracking-wider mb-1">Match</p>
                    <Badge variant={matchVariant(reviewItem.match_quality)}>
                      {reviewItem.match_quality ?? 'unknown'}
                    </Badge>
                  </div>
                </div>

                <div className="flex gap-4">
                  <Button
                    variant="ghost"
                    onClick={() => {
                      handleSkip(reviewItem.id, reviewItem.artist, reviewItem.title)
                    }}
                    disabled={actionInProgress.has(reviewItem.id)}
                  >
                    <XCircle className="w-4 h-4" />
                    Skip (S)
                  </Button>
                  <Button
                    variant="primary"
                    onClick={() => {
                      handleApprove(reviewItem.id, reviewItem.artist, reviewItem.title)
                    }}
                    disabled={actionInProgress.has(reviewItem.id)}
                  >
                    <CheckCircle className="w-4 h-4" />
                    Approve (A)
                  </Button>
                </div>
              </GlassCard>
            )}

            <div className="flex gap-4">
              <Button variant="secondary" onClick={() => setReviewIndex(i => Math.max(i - 1, 0))} disabled={reviewIndex === 0}>← Prev</Button>
              <Button variant="secondary" onClick={() => setReviewIndex(i => Math.min(i + 1, foundItems.length - 1))} disabled={reviewIndex >= foundItems.length - 1}>Next →</Button>
            </div>
          </div>
        )
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
      {/* Scan Launcher Modal */}
      {scanModalOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
          onClick={() => setScanModalOpen(false)}
        >
          <div
            className="bg-base-800 border border-glass-border rounded-2xl p-6 w-full max-w-md shadow-2xl"
            onClick={e => e.stopPropagation()}
          >
            <h3 className="text-lg font-semibold mb-4">Find Upgrades</h3>

            <div className="space-y-4">
              {/* Format filter */}
              <div>
                <label className="text-xs text-base-400 uppercase tracking-wider mb-2 block">Format</label>
                <div className="flex flex-wrap gap-2">
                  {[
                    { value: 'all_lossy', label: 'All Lossy' },
                    { value: 'mp3', label: 'MP3' },
                    { value: 'aac', label: 'AAC' },
                    { value: 'cd_flac', label: 'CD FLAC → Hi-Res' },
                  ].map(opt => (
                    <button
                      key={opt.value}
                      onClick={() => setScanScope(s => ({ ...s, format_filter: opt.value }))}
                      className={`px-3 py-1.5 rounded-lg text-sm font-medium border transition-all ${
                        scanScope.format_filter === opt.value
                          ? 'bg-lime/20 border-lime/40 text-lime'
                          : 'bg-base-700 border-base-600 text-base-400 hover:text-base-200'
                      }`}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* Unscanned only toggle */}
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium">Unscanned only</p>
                  <p className="text-xs text-base-500">Skip tracks already searched</p>
                </div>
                <button
                  onClick={() => setScanScope(s => ({ ...s, unscanned_only: !s.unscanned_only }))}
                  className={`w-11 h-6 rounded-full transition-all relative ${scanScope.unscanned_only ? 'bg-lime' : 'bg-base-600'}`}
                >
                  <span className={`absolute top-0.5 w-5 h-5 rounded-full bg-white shadow transition-all ${scanScope.unscanned_only ? 'left-5' : 'left-0.5'}`} />
                </button>
              </div>

              {/* Batch size */}
              <div>
                <label className="text-xs text-base-400 uppercase tracking-wider mb-1 block">
                  Batch size (albums per run)
                </label>
                <input
                  type="number"
                  min={0}
                  max={500}
                  value={scanScope.batch_size}
                  onChange={e => setScanScope(s => ({ ...s, batch_size: parseInt(e.target.value) || 50 }))}
                  className="w-full bg-base-700 border border-base-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-lime/50"
                />
              </div>

              {/* Artist filter */}
              <div>
                <label className="text-xs text-base-400 uppercase tracking-wider mb-1 block">
                  Artist filter (optional)
                </label>
                <input
                  type="text"
                  placeholder="e.g. Pink Floyd"
                  value={scanScope.artist_filter}
                  onChange={e => setScanScope(s => ({ ...s, artist_filter: e.target.value }))}
                  className="w-full bg-base-700 border border-base-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-lime/50 placeholder:text-base-600"
                />
              </div>
            </div>

            <div className="flex gap-3 mt-6">
              <button
                onClick={() => setScanModalOpen(false)}
                className="flex-1 px-4 py-2 rounded-xl text-sm font-medium bg-base-700 text-base-400 hover:bg-base-600 border border-base-600 transition-all"
              >
                Cancel
              </button>
              <button
                onClick={handleScan}
                className="flex-1 px-4 py-2 rounded-xl text-sm font-semibold bg-lime text-white hover:bg-lime/90 transition-all inline-flex items-center justify-center gap-2"
              >
                <Search className="w-4 h-4" />
                Start Scan
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
