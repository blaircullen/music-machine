import { useEffect, useState, useCallback, useRef } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import { Wand2, CheckCircle, XCircle, SkipForward, RefreshCw, Loader2, Clock, FileAudio } from 'lucide-react'
import { GlassCard, StatCard, Button, Badge, ProgressBar, EmptyState, SkeletonTable, toast } from '../components/ui'
import { useTaggerStatus } from '../hooks/useTaggerStatus'

interface TaggerResult {
  id: number
  track_id: number | null
  file_path: string
  status: string
  acoustid_score: number | null
  mb_recording_id: string | null
  matched_artist: string | null
  matched_title: string | null
  matched_album: string | null
  cover_art_url: string | null
  error_msg: string | null
  created_at: string
  updated_at: string
}

type FilterTab = 'all' | 'tagged' | 'failed' | 'skipped' | 'pending' | 'matched'

const statusVariant = (s: string) => {
  const map: Record<string, 'default' | 'green' | 'red' | 'gray' | 'blue' | 'amber'> = {
    pending: 'default',
    matched: 'blue',
    tagged: 'green',
    failed: 'red',
    skipped: 'gray',
  }
  return map[s] ?? 'default'
}

function formatElapsed(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  if (m === 0) return `${s}s`
  return `${m}m ${s}s`
}

function truncatePath(path: string, maxLen = 50): string {
  if (path.length <= maxLen) return path
  return '...' + path.slice(-(maxLen - 3))
}

export default function Tagger() {
  const [results, setResults] = useState<TaggerResult[]>([])
  const [loading, setLoading] = useState(true)
  const [filterTab, setFilterTab] = useState<FilterTab>('all')
  const [taggerRequested, setTaggerRequested] = useState(false)
  const [actionInProgress, setActionInProgress] = useState<Set<number>>(new Set())

  const { status: taggerStatus } = useTaggerStatus()
  const prevPhaseRef = useRef(taggerStatus.phase)

  const isRunning = taggerStatus.running || taggerRequested

  const fetchResults = useCallback(async () => {
    setLoading(true)
    try {
      const params = filterTab === 'all' ? '' : `?status=${filterTab}`
      const res = await fetch(`/api/tagger/results${params}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data: TaggerResult[] = await res.json()
      setResults(data)
    } catch {
      toast.error('Failed to load tagger results')
      setResults([])
    } finally {
      setLoading(false)
    }
  }, [filterTab])

  useEffect(() => {
    fetchResults()
  }, [fetchResults])

  // React to phase transitions
  useEffect(() => {
    const prev = prevPhaseRef.current
    prevPhaseRef.current = taggerStatus.phase

    if (taggerStatus.phase === 'tagging' || taggerStatus.phase === 'scanning') {
      setTaggerRequested(false)
    }

    const justFinished = !taggerStatus.running && prev !== taggerStatus.phase
    if (justFinished && (taggerStatus.phase === 'complete' || taggerStatus.phase === 'failed' || taggerStatus.phase === 'idle')) {
      if (prev === 'tagging' || prev === 'scanning') {
        const t = taggerStatus.tagged
        const f = taggerStatus.failed
        const s = taggerStatus.skipped
        if (t === 0 && f === 0 && s > 0) {
          toast.success('All files already tagged')
        } else if (f > 0 && t > 0) {
          toast.success(`Tagged ${t}, ${f} failed, ${s} skipped`)
        } else if (t > 0) {
          toast.success(`Successfully tagged ${t} file${t === 1 ? '' : 's'}`)
        } else if (f > 0) {
          toast.error(`All ${f} file${f === 1 ? '' : 's'} failed`)
        }
      }
      setTaggerRequested(false)
      fetchResults()
    }
  }, [taggerStatus.phase, taggerStatus.running, fetchResults, taggerStatus.tagged, taggerStatus.failed, taggerStatus.skipped])

  const handleStart = async () => {
    setTaggerRequested(true)
    try {
      const res = await fetch('/api/tagger/run', { method: 'POST' })
      const data = await res.json()
      if (data.error) {
        setTaggerRequested(false)
        toast.error(data.error)
        return
      }
      toast.success('Tagger started')
    } catch {
      setTaggerRequested(false)
      toast.error('Failed to start tagger')
    }
  }

  const handleRetry = useCallback(async (id: number) => {
    setActionInProgress(prev => new Set(prev).add(id))
    try {
      const res = await fetch(`/api/tagger/${id}/retry`, { method: 'POST' })
      const data = await res.json()
      if (!data.ok) {
        toast.error(data.error || 'Retry failed')
        return
      }
      setResults(prev => prev.map(r =>
        r.id === id ? { ...r, status: data.status } : r
      ))
      toast.success('Retry successful')
    } catch {
      toast.error('Retry failed')
    } finally {
      setActionInProgress(prev => { const next = new Set(prev); next.delete(id); return next })
    }
  }, [])

  const handleSkip = useCallback(async (id: number) => {
    setActionInProgress(prev => new Set(prev).add(id))
    try {
      await fetch(`/api/tagger/${id}/skip`, { method: 'POST' })
      setResults(prev => prev.map(r =>
        r.id === id ? { ...r, status: 'skipped' } : r
      ))
    } catch {
      toast.error('Skip failed')
    } finally {
      setActionInProgress(prev => { const next = new Set(prev); next.delete(id); return next })
    }
  }, [])

  const taggedCount = results.filter(r => r.status === 'tagged').length
  const failedCount = results.filter(r => r.status === 'failed').length
  const skippedCount = results.filter(r => r.status === 'skipped').length
  const pendingCount = results.filter(r => r.status === 'pending' || r.status === 'matched').length

  const tabs: { key: FilterTab; label: string; count: number }[] = [
    { key: 'all', label: 'All', count: results.length },
    { key: 'tagged', label: 'Tagged', count: taggedCount },
    { key: 'failed', label: 'Failed', count: failedCount },
    { key: 'skipped', label: 'Skipped', count: skippedCount },
    { key: 'pending', label: 'Pending', count: pendingCount },
  ]

  const fileName = (path: string) => {
    const parts = path.split('/')
    return parts[parts.length - 1] || path
  }

  const dirName = (path: string) => {
    const parts = path.split('/')
    if (parts.length >= 2) {
      return parts[parts.length - 2]
    }
    return ''
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-2xl font-bold font-[family-name:var(--font-family-display)]">MetaTagger</h2>
          <p className="text-xs text-base-500 mt-1">
            Identify tracks via acoustic fingerprint and enrich metadata from MusicBrainz
          </p>
        </div>
        <button
          onClick={handleStart}
          disabled={isRunning}
          className="px-4 py-2 rounded-xl text-sm font-medium transition-all duration-300 inline-flex items-center gap-2 bg-base-700/80 text-base-300 hover:bg-base-600 border border-glass-border backdrop-blur-md disabled:opacity-40 disabled:cursor-not-allowed shadow-sm hover:shadow-md"
        >
          {isRunning
            ? <Loader2 className="w-4 h-4 animate-spin text-[#0ea5e9]" />
            : <Wand2 className="w-4 h-4 text-[#0ea5e9]" />
          }
          {isRunning ? 'Tagging...' : 'Start Tagger'}
        </button>
      </div>

      {/* Progress panel */}
      <AnimatePresence>
        {isRunning && (
          <motion.div
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8, transition: { duration: 0.2 } }}
          >
            <GlassCard className="p-5 border-[#0ea5e9]/20 shadow-[0_0_20px_rgba(14,165,233,0.1)] relative overflow-hidden">
              <div className="absolute inset-0 bg-gradient-to-r from-transparent via-[#0ea5e9]/5 to-transparent -translate-x-full animate-[shimmer_2s_infinite]" />

              {/* Header row */}
              <div className="flex items-center justify-between mb-4 relative z-10">
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-xl bg-[#0ea5e9]/15 border border-[#0ea5e9]/20 shadow-[0_0_10px_rgba(14,165,233,0.2)]">
                    <Wand2 className="w-4 h-4 text-[#7dd3fc] animate-pulse" />
                  </div>
                  <div>
                    <p className="text-sm font-semibold text-base-200">
                      {taggerStatus.phase === 'scanning' ? 'Scanning files...' : 'Tagging tracks'}
                    </p>
                    <p className="text-xs text-base-400">
                      {taggerStatus.total > 0
                        ? `${taggerStatus.processed.toLocaleString()} of ${taggerStatus.total.toLocaleString()} files`
                        : 'Starting...'
                      }
                    </p>
                  </div>
                </div>

                {/* Live counters + elapsed */}
                <div className="flex items-center gap-4 text-xs text-base-400">
                  {taggerStatus.tagged > 0 && (
                    <span className="text-[#4ade80] font-medium">{taggerStatus.tagged} tagged</span>
                  )}
                  {taggerStatus.failed > 0 && (
                    <span className="text-[#f87171] font-medium">{taggerStatus.failed} failed</span>
                  )}
                  {taggerStatus.skipped > 0 && (
                    <span className="text-base-400">{taggerStatus.skipped} skipped</span>
                  )}
                  {taggerStatus.elapsed_s > 0 && (
                    <span className="flex items-center gap-1 text-base-500 font-mono">
                      <Clock className="w-3 h-3" />
                      {formatElapsed(taggerStatus.elapsed_s)}
                    </span>
                  )}
                </div>
              </div>

              {/* Current file */}
              {taggerStatus.current_file && (
                <div className="flex items-center gap-1.5 mb-3 relative z-10">
                  <FileAudio className="w-3.5 h-3.5 text-base-500 shrink-0" />
                  <span className="text-xs text-base-500 font-mono truncate">
                    {truncatePath(taggerStatus.current_file)}
                  </span>
                </div>
              )}

              {/* Progress bar */}
              <div className="relative z-10">
                <ProgressBar
                  value={taggerStatus.processed}
                  max={taggerStatus.total}
                  active={taggerStatus.total === 0}
                />
              </div>
            </GlassCard>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Stat cards */}
      <div className="grid grid-cols-4 gap-4">
        <StatCard
          icon={Wand2}
          label="Tagged"
          value={(isRunning ? taggerStatus.tagged : taggedCount).toLocaleString()}
          accent="green"
        />
        <StatCard
          icon={XCircle}
          label="Failed"
          value={(isRunning ? taggerStatus.failed : failedCount).toLocaleString()}
          accent="red"
        />
        <StatCard
          icon={SkipForward}
          label="Skipped"
          value={(isRunning ? taggerStatus.skipped : skippedCount).toLocaleString()}
          accent="amber"
        />
        <StatCard
          icon={CheckCircle}
          label="Total"
          value={(isRunning ? taggerStatus.total : results.length).toLocaleString()}
          accent="blue"
        />
      </div>

      {/* Filter tabs */}
      <div className="flex gap-2">
        {tabs.map(tab => (
          <button
            key={tab.key}
            onClick={() => setFilterTab(tab.key)}
            className={`px-3 py-1.5 rounded-xl text-sm font-medium transition-all duration-300 flex items-center gap-2 relative ${
              filterTab === tab.key
                ? 'text-[#7dd3fc] drop-shadow-[0_0_8px_rgba(14,165,233,0.5)]'
                : 'text-base-500 hover:text-base-300 hover:bg-base-700/50'
            }`}
          >
            {tab.label}
            {tab.count > 0 && (
              <span className={`text-xs px-1.5 py-0.5 rounded-md ${
                filterTab === tab.key
                  ? 'bg-[#0ea5e9]/20 text-[#7dd3fc] font-bold'
                  : 'bg-base-800/80'
              }`}>
                {tab.count}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Results table */}
      {loading ? (
        <SkeletonTable rows={6} cols={7} />
      ) : results.length === 0 ? (
        <EmptyState
          icon={Wand2}
          title="No tagging results"
          description="Click Start Tagger to identify and enrich your music library metadata."
        />
      ) : (
        <GlassCard className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-glass-border text-base-400 text-left">
                  <th className="px-4 py-3 font-medium">File</th>
                  <th className="px-4 py-3 font-medium">Matched Artist</th>
                  <th className="px-4 py-3 font-medium">Matched Title</th>
                  <th className="px-4 py-3 font-medium">Album</th>
                  <th className="px-4 py-3 font-medium text-center">Score</th>
                  <th className="px-4 py-3 font-medium text-center">Status</th>
                  <th className="px-4 py-3 font-medium text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {results.map(item => {
                  const busy = actionInProgress.has(item.id)
                  return (
                    <tr key={item.id} className="border-b border-glass-border/30 hover:bg-white/[0.02] transition-colors duration-200">
                      <td className="px-4 py-4 max-w-[220px]">
                        <div className="truncate text-base-300 font-medium text-sm" title={item.file_path}>
                          {fileName(item.file_path)}
                        </div>
                        <div className="truncate text-base-500 text-xs mt-0.5">{dirName(item.file_path)}</div>
                      </td>
                      <td className="px-4 py-4 text-base-300 font-medium">{item.matched_artist || '—'}</td>
                      <td className="px-4 py-4 text-base-400">{item.matched_title || '—'}</td>
                      <td className="px-4 py-4 text-base-400 max-w-[160px] truncate">{item.matched_album || '—'}</td>
                      <td className="px-4 py-4 text-center">
                        {item.acoustid_score != null
                          ? (
                            <span className={`font-mono text-xs ${
                              item.acoustid_score >= 0.9 ? 'text-[#4ade80]'
                              : item.acoustid_score >= 0.7 ? 'text-base-300'
                              : 'text-[#fbbf24]'
                            }`}>
                              {(item.acoustid_score * 100).toFixed(0)}%
                            </span>
                          )
                          : <span className="text-base-600 text-xs">—</span>
                        }
                      </td>
                      <td className="px-4 py-4 text-center">
                        <Badge variant={statusVariant(item.status)}>{item.status}</Badge>
                      </td>
                      <td className="px-4 py-4 text-right">
                        {item.status === 'failed' && (
                          <div className="flex gap-2 justify-end">
                            <Button
                              size="sm"
                              variant="secondary"
                              onClick={() => handleRetry(item.id)}
                              disabled={busy}
                            >
                              <RefreshCw className="w-3 h-3" />
                              Retry
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => handleSkip(item.id)}
                              disabled={busy}
                            >
                              Skip
                            </Button>
                          </div>
                        )}
                        {item.error_msg && (
                          <p className="text-[11px] text-red-400/70 mt-1 max-w-[200px] truncate" title={item.error_msg}>
                            {item.error_msg}
                          </p>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </GlassCard>
      )}
    </div>
  )
}
