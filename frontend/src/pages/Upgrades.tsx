import { useEffect, useState, useCallback, useRef } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import { ArrowUpCircle, Search, Download, CheckCircle, XCircle, Loader2 } from 'lucide-react'
import { GlassCard, StatCard, Button, Badge, ProgressBar, EmptyState, SkeletonTable, toast } from '../components/ui'
import { useUpgradeStatus } from '../hooks/useUpgradeStatus'

interface QueueItem {
  id: number
  track_id: number
  search_query: string
  status: string
  match_type: string | null
  created_at: string
  artist: string
  title: string
  album: string
  format: string
  bitrate: number
}

type FilterTab = 'all' | 'pending' | 'approved' | 'completed' | 'skipped'

const matchVariant = (m: string | null) => {
  if (m === 'exact') return 'green' as const
  if (m === 'fuzzy') return 'amber' as const
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

  const { status: upgradeStatus } = useUpgradeStatus()
  const prevPhaseRef = useRef(upgradeStatus.phase)

  const isDownloading = upgradeStatus.phase === 'downloading' || downloadRequested
  const isSearching = upgradeStatus.phase === 'searching' || searchRequested

  const fetchQueue = useCallback(async () => {
    setLoading(true)
    try {
      const params = filterTab === 'all' ? '' : `?status=${filterTab}`
      const res = await fetch(`/api/upgrades/queue${params}`)
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
    if (prev !== 'idle' && upgradeStatus.phase === 'idle') {
      if (prev === 'searching') toast.success('Search complete')
      if (prev === 'downloading') toast.success('Downloads complete')
      setDownloadRequested(false)
      setSearchRequested(false)
      fetchQueue()
    }

  }, [upgradeStatus.phase, upgradeStatus.running, fetchQueue])

  const handleScan = async () => {
    setSearchRequested(true)
    try {
      await fetch('/api/upgrades/scan', { method: 'POST' })
      toast.success('Search started')
    } catch {
      setSearchRequested(false)
      toast.error('Failed to start upgrade scan')
    }
  }

  const handleApprove = async (id: number, artist: string, title: string) => {
    setActionInProgress(prev => new Set(prev).add(id))
    try {
      const res = await fetch(`/api/upgrades/queue/${id}/approve`, { method: 'POST' })
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
      await fetch(`/api/upgrades/queue/${id}/skip`, { method: 'POST' })
      setQueue(prev => prev.filter(item => item.id !== id))
      toast(`Skipped: ${artist} - ${title}`, { icon: '⏭' })
    } catch {
      toast.error('Failed to skip')
    } finally {
      setActionInProgress(prev => { const next = new Set(prev); next.delete(id); return next })
    }
  }

  const handleApproveAllExact = async () => {
    const count = exactPendingCount
    try {
      await fetch('/api/upgrades/approve-all-exact', { method: 'POST' })
      setQueue(prev => prev.map(item =>
        item.match_type === 'exact' && item.status === 'pending'
          ? { ...item, status: 'approved' }
          : item
      ))
      toast.success(`Approved ${count} exact matches`)
    } catch {
      toast.error('Failed to approve all exact')
    }
  }

  const handleDownloadApproved = async () => {
    setDownloadRequested(true)
    try {
      const res = await fetch('/api/upgrades/download-approved', { method: 'POST' })
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
  const exactMatches = queue.filter(i => i.match_type === 'exact').length
  const approvedCount = queue.filter(i => i.status === 'approved').length
  const exactPendingCount = queue.filter(i => i.match_type === 'exact' && i.status === 'pending').length

  const tabs: { key: FilterTab; label: string; count: number }[] = [
    { key: 'all', label: 'All', count: queue.length },
    { key: 'pending', label: 'Pending', count: queue.filter(i => i.status === 'pending').length },
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
          {exactPendingCount > 0 && (
            <Button variant="secondary" onClick={handleApproveAllExact}>
              <CheckCircle className="w-4 h-4" />
              Approve All Exact ({exactPendingCount})
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
            value={upgradeStatus.progress}
            max={upgradeStatus.total}
            label={upgradeStatus.total > 0
              ? `Searching for upgrades... ${upgradeStatus.progress}/${upgradeStatus.total}`
              : 'Searching for upgrades...'
            }
            active
          />
        </GlassCard>
      )}

      {/* Download progress panel */}
      {isDownloading && (
        <GlassCard className="p-5 border-lime/30 shadow-[0_0_20px_rgba(16,185,129,0.15)] relative overflow-hidden">
          {/* Subtle animated background gradient */}
          <div className="absolute inset-0 bg-gradient-to-r from-transparent via-lime/5 to-transparent -translate-x-full animate-[shimmer_2s_infinite]" />

          <div className="flex items-center gap-3 mb-3 relative z-10">
            <div className="p-2 rounded-xl bg-lime-dim border border-lime/20 shadow-[0_0_10px_rgba(16,185,129,0.2)]">
              <Download className="w-4 h-4 text-lime animate-bounce" />
            </div>
            <div>
              <p className="text-sm font-medium text-base-300">Downloading FLAC upgrades</p>
              <p className="text-xs text-base-400">
                {upgradeStatus.total > 0
                  ? `Track ${upgradeStatus.progress} of ${upgradeStatus.total}`
                  : 'Starting downloads...'
                }
              </p>
            </div>
          </div>
          <div className="relative z-10">
            <ProgressBar
              value={upgradeStatus.progress}
              max={upgradeStatus.total}
              active
            />
          </div>
          {upgradeStatus.current && (
            <p className="text-xs text-base-400 mt-2 truncate relative z-10">
              <span className="text-lime/80 font-medium">Downloading:</span> {upgradeStatus.current}
            </p>
          )}
        </GlassCard>
      )}

      <div className="grid grid-cols-3 gap-4">
        <StatCard icon={ArrowUpCircle} label="Candidates" value={totalCandidates.toLocaleString()} />
        <StatCard icon={CheckCircle} label="Exact Matches" value={exactMatches.toLocaleString()} />
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
                  <th className="px-4 py-3 font-medium">Artist</th>
                  <th className="px-4 py-3 font-medium">Album</th>
                  <th className="px-4 py-3 font-medium">Title</th>
                  <th className="px-4 py-3 font-medium">Format</th>
                  <th className="px-4 py-3 font-medium text-center">Match</th>
                  <th className="px-4 py-3 font-medium text-center">Status</th>
                  <th className="px-4 py-3 font-medium text-right">Actions</th>
                </tr>
              </thead>
              <AnimatePresence>
                <tbody>
                  {queue.map(item => {
                    const busy = actionInProgress.has(item.id)
                    const canAct = item.status === 'pending'
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
                          <Badge variant={matchVariant(item.match_type)}>{item.match_type ?? 'pending'}</Badge>
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
