import { useState, useEffect, useRef, useCallback } from 'react'
import { Play, Square, ChevronDown, ChevronUp, ChevronLeft, ChevronRight, AlertTriangle, Check, X, Wand2 } from 'lucide-react'
import { GlassCard } from '../components/ui/GlassCard'
import { Button } from '../components/ui/Button'
import { Badge } from '../components/ui/Badge'
import {
  getIdentityReport, getIdentitySweepStatus, startIdentitySweep, stopIdentitySweep,
  getIdentityTracks, reviewIdentityTrack, applyApprovedIdentity,
  type IdentityReport as IdentityReportData,
  type IdentitySweepStatus, type IdentityTrackItem, type IdentityBucket,
} from '../lib/api'

const PAGE_SIZE = 50

const BUCKETS: Array<{ key: IdentityBucket; label: string }> = [
  { key: 'confirmed', label: 'Confirmed' },
  { key: 'review', label: 'Review' },
  { key: 'conflict', label: 'Conflict' },
  { key: 'unknown', label: 'Unknown' },
  { key: 'deferred', label: 'Deferred' },
  { key: 'error', label: 'Error' },
  { key: 'divergent', label: 'Divergent' },
]

function StateBadge({ state }: { state: string }) {
  const variant =
    state === 'confirmed' ? 'green' as const :
    state === 'review' ? 'amber' as const :
    state === 'conflict' || state === 'error' ? 'orange' as const :
    'default' as const
  return <Badge variant={variant}>{state}</Badge>
}

function bucketCount(report: IdentityReportData | null, bucket: IdentityBucket): number {
  if (!report) return 0
  if (bucket === 'divergent') return report.divergent
  return report.by_state[bucket] ?? 0
}

export default function IdentityReport() {
  const [report, setReport] = useState<IdentityReportData | null>(null)
  const [sweep, setSweep] = useState<IdentitySweepStatus | null>(null)
  const [bucket, setBucket] = useState<IdentityBucket>('review')
  const [items, setItems] = useState<IdentityTrackItem[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(true)
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [sweepRequested, setSweepRequested] = useState(false)
  const [actioning, setActioning] = useState<number | null>(null)
  const [applyMsg, setApplyMsg] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadReport = useCallback(async () => {
    try {
      const [r, s] = await Promise.all([getIdentityReport(), getIdentitySweepStatus()])
      setReport(r)
      setSweep(s)
      if (s.running) setSweepRequested(false)
    } catch {
      // ignore
    }
  }, [])

  const loadBucket = useCallback(async (b: IdentityBucket, off: number) => {
    setLoading(true)
    try {
      const data = await getIdentityTracks(b, PAGE_SIZE, off)
      setItems(data.items)
      setTotal(data.total)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { loadReport() }, [loadReport])
  useEffect(() => { loadBucket(bucket, offset) }, [bucket, offset, loadBucket])

  // Poll while a sweep is running (2s, same cadence as other pages)
  useEffect(() => {
    if (sweep?.running || sweepRequested) {
      if (!pollRef.current) {
        pollRef.current = setInterval(loadReport, 2000)
      }
    } else if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
      loadBucket(bucket, offset)
    }
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [sweep?.running, sweepRequested, loadReport, loadBucket, bucket, offset])

  const handleStart = async () => {
    setSweepRequested(true)
    try {
      const res = await startIdentitySweep()
      if (!res.ok) setSweepRequested(false)
    } catch {
      setSweepRequested(false)
    }
    loadReport()
  }

  const handleStop = async () => {
    try { await stopIdentitySweep() } catch { /* ignore */ }
    loadReport()
  }

  const handleReview = async (trackId: number, decision: 'approve' | 'reject') => {
    setActioning(trackId)
    try {
      const res = await reviewIdentityTrack(trackId, decision)
      if (res.ok) {
        setItems((prev) => prev.filter((it) => it.track_id !== trackId))
        setTotal((t) => Math.max(0, t - 1))
        setExpandedId(null)
      }
    } catch {
      // ignore
    } finally {
      setActioning(null)
    }
  }

  const handleApplyApproved = async () => {
    setApplyMsg('Applying approved…')
    try {
      const res = await applyApprovedIdentity(['artist', 'title', 'album'])
      if (!res.ok) { setApplyMsg(`Error: ${res.error ?? 'failed'}`); return }
      if (!res.summary) { setApplyMsg(res.note ?? 'No approved rows pending'); return }
      setApplyMsg(`Applied ${res.summary.applied}, ${res.summary.errors} error(s)`)
      loadReport()
      loadBucket(bucket, offset)
    } catch {
      setApplyMsg('Apply failed')
    }
  }

  const selectBucket = (b: IdentityBucket) => {
    setBucket(b)
    setOffset(0)
    setExpandedId(null)
  }

  const running = sweep?.running || sweepRequested
  const page = Math.floor(offset / PAGE_SIZE) + 1
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <div className="space-y-4 max-w-6xl">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-bold text-white font-[family-name:var(--font-family-display)]">
            Identity Report
          </h1>
          {report && <Badge variant="default">{report.total_resolved} / {report.total_active} resolved</Badge>}
        </div>
        <div className="flex gap-2">
          {bucket === 'review' && (
            <Button onClick={handleApplyApproved} size="sm" variant="secondary">
              <Wand2 className="w-3.5 h-3.5 mr-1.5" />
              Apply approved
            </Button>
          )}
          {running ? (
            <Button onClick={handleStop} size="sm" variant="secondary">
              <Square className="w-3.5 h-3.5 mr-1.5" />
              Stop Sweep
            </Button>
          ) : (
            <Button onClick={handleStart} size="sm">
              <Play className="w-3.5 h-3.5 mr-1.5" />
              Run Sweep
            </Button>
          )}
        </div>
      </div>

      {applyMsg && (
        <div className="text-xs text-amber-300">{applyMsg}</div>
      )}

      {/* Sweep progress */}
      {running && sweep && (
        <GlassCard>
          <div className="flex items-center justify-between text-sm">
            <span className="text-amber-300">
              Sweeping… {sweep.processed} / {sweep.total}
            </span>
            <span className="text-xs text-slate-500 truncate max-w-[50%]">{sweep.current_file}</span>
          </div>
          <div className="mt-2 h-1.5 bg-slate-800 rounded-full overflow-hidden">
            <div
              className="h-full bg-[#d4a017] transition-all"
              style={{ width: sweep.total ? `${(sweep.processed / sweep.total) * 100}%` : '0%' }}
            />
          </div>
        </GlassCard>
      )}

      {sweep && !sweep.running && sweep.stopped_reason && sweep.stopped_reason !== 'complete' && (
        <GlassCard>
          <div className="flex items-center gap-2 text-sm text-orange-300">
            <AlertTriangle className="w-4 h-4" />
            Last sweep stopped: {sweep.stopped_reason}
          </div>
        </GlassCard>
      )}

      {/* Bucket tabs */}
      <div className="flex gap-1.5 flex-wrap">
        {BUCKETS.map(({ key, label }) => (
          <button
            key={key}
            onClick={() => selectBucket(key)}
            className={[
              'px-3 py-1.5 rounded-lg text-xs font-medium transition-colors',
              bucket === key
                ? 'text-[#f0c95c] bg-[#d4a017]/10 border border-[#d4a017]/40'
                : 'text-slate-400 hover:text-slate-200 bg-slate-800/40 border border-transparent',
            ].join(' ')}
          >
            {label}
            <span className="ml-1.5 text-slate-500">{bucketCount(report, key)}</span>
          </button>
        ))}
      </div>

      {/* Track table */}
      {loading ? (
        <div className="flex items-center justify-center h-40">
          <div className="w-5 h-5 border-2 border-[#d4a017] border-t-transparent rounded-full animate-spin" />
        </div>
      ) : items.length === 0 ? (
        <GlassCard>
          <div className="text-center py-8">
            <p className="text-slate-400">No tracks in this bucket</p>
          </div>
        </GlassCard>
      ) : (
        <div className="space-y-2">
          <div className="grid grid-cols-[1fr_1fr_70px_70px_24px] gap-2 px-3 text-xs text-slate-500 font-medium">
            <span>Current Tags</span>
            <span>Resolved Identity</span>
            <span className="text-right">Tier</span>
            <span className="text-right">State</span>
            <span />
          </div>

          {items.map((item) => (
            <GlassCard key={item.track_id} className="!p-0">
              <div
                className="grid grid-cols-[1fr_1fr_70px_70px_24px] gap-2 items-center px-3 py-2.5 cursor-pointer hover:bg-white/[0.02] transition-colors"
                onClick={() => setExpandedId(expandedId === item.track_id ? null : item.track_id)}
              >
                <div className="min-w-0">
                  <div className="text-sm text-white truncate">
                    {item.tag_artist || '—'} — {item.tag_title || '—'}
                  </div>
                  <div className="text-xs text-slate-500 truncate">{item.tag_album || ''}</div>
                </div>
                <div className="min-w-0">
                  <div className="text-sm text-amber-200 truncate">
                    {item.artist ? `${item.artist} — ${item.title}` : '—'}
                  </div>
                  <div className="text-xs text-slate-500 truncate">{item.album || ''}</div>
                </div>
                <div className="text-right text-xs text-slate-400">{item.tier || '—'}</div>
                <div className="text-right">
                  <StateBadge state={item.state} />
                </div>
                <div className="flex justify-end">
                  {expandedId === item.track_id
                    ? <ChevronUp className="w-4 h-4 text-slate-500" />
                    : <ChevronDown className="w-4 h-4 text-slate-500" />}
                </div>
              </div>

              {expandedId === item.track_id && (
                <div className="border-t border-slate-800 px-4 py-3 bg-slate-900/50 space-y-2">
                  <div className="text-xs text-slate-400 grid grid-cols-2 gap-x-6 gap-y-1">
                    <span>Recording: <span className="text-slate-300">{item.mb_recording_id || '—'}</span></span>
                    <span>ISRC: <span className="text-slate-300">{item.isrc || '—'}</span></span>
                    <span>Divergent: <span className="text-slate-300">{item.divergent ? 'yes' : 'no'}</span></span>
                    <span>Decided: <span className="text-slate-300">{item.decided_at}</span></span>
                  </div>
                  <div className="text-xs text-slate-600 truncate">{item.file_path}</div>
                  {(bucket === 'review' || bucket === 'conflict') && item.artist && (
                    <div className="flex items-center gap-2 pt-1">
                      <Button size="sm" disabled={actioning === item.track_id}
                              onClick={() => handleReview(item.track_id, 'approve')}>
                        <Check className="w-3.5 h-3.5 mr-1.5" />
                        Approve
                      </Button>
                      <Button size="sm" variant="secondary" disabled={actioning === item.track_id}
                              onClick={() => handleReview(item.track_id, 'reject')}>
                        <X className="w-3.5 h-3.5 mr-1.5" />
                        Reject
                      </Button>
                      <span className="text-[11px] text-slate-500">
                        Approve marks it; click “Apply approved” to write tags.
                      </span>
                    </div>
                  )}
                  <pre className="text-[11px] text-slate-400 bg-slate-950/60 rounded-lg p-3 overflow-x-auto max-h-72 overflow-y-auto">
                    {(() => {
                      try { return JSON.stringify(JSON.parse(item.evidence), null, 2) }
                      catch { return item.evidence }
                    })()}
                  </pre>
                </div>
              )}
            </GlassCard>
          ))}

          {/* Pagination */}
          {pages > 1 && (
            <div className="flex items-center justify-end gap-2 text-xs text-slate-400 pt-1">
              <button
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                disabled={offset === 0}
                className="p-1 rounded hover:bg-slate-800 disabled:opacity-40"
                aria-label="Previous page"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <span>{page} / {pages}</span>
              <button
                onClick={() => setOffset(offset + PAGE_SIZE)}
                disabled={offset + PAGE_SIZE >= total}
                className="p-1 rounded hover:bg-slate-800 disabled:opacity-40"
                aria-label="Next page"
              >
                <ChevronRight className="w-4 h-4" />
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
